"""Local image catalogue, background painting generation, and replay server."""
import argparse
import errno
import hashlib
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from urllib.parse import quote, urlsplit
import uuid
import webbrowser

from refine import ROOT, EXTENSIONS, BRUSHES, DIRECTIONS, RENDERER_VERSION, generate, input_path, validate_settings
from painting_settings import PresetStore, DEFAULTS, CHOICES, NUMBERS, normalize_settings, classify_preset

API_VERSION = 4
RESTART_MESSAGE = '服务代码已更新，请关闭旧 CMD 窗口，重新运行 run.bat，并使用新打开的网页。'


def source_revision():
    digest = hashlib.sha256()
    for path in [Path(__file__), ROOT / 'refine.py', ROOT / 'painting_settings.py', ROOT / 'web/brush.js', ROOT / 'web/refine.js']:
        digest.update(path.read_bytes())
    return digest.hexdigest()


LOADED_REVISION = source_revision()


def catalogue(root):
    items = []
    for path in sorted((root / 'input').iterdir(), key=lambda p:p.name.casefold()):
        if not path.is_file() or path.is_symlink() or path.suffix.lower() not in EXTENSIONS:
            continue
        variants = []
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        image_folder = root / 'output' / path.name
        for metadata in image_folder.rglob('generation.json'):
            relative = metadata.parent.relative_to(root / 'output')
            if any(part.startswith('.') for part in relative.parts):
                continue
            try:
                meta = json.loads(metadata.read_text(encoding='utf-8'))
                if ((metadata.parent / 'strokes.json').is_file() and meta['sourceSHA256'] == digest
                        and meta.get('rendererVersion') == RENDERER_VERSION):
                    variants.append(dict(brush=meta['brush'], direction=meta['direction'],
                        stages=meta.get('stageCount'), minDiameter=meta.get('minDiameter'),
                        outputURL='/output/'+quote(relative.as_posix(), safe='/')+'/'))
            except (OSError, ValueError, KeyError):
                pass
        items.append(dict(name=path.name, hasOutput=bool(variants), variants=variants))
    return items


def saved_outputs(root):
    """List saved paintings independently of whether the original still exists."""
    folder = root / 'output'
    if not folder.exists():
        return []
    items = []
    for path in sorted(folder.rglob('strokes.json')):
        relative = path.parent.relative_to(folder)
        if any(part.startswith('.') for part in relative.parts):
            continue
        try:
            if not path.resolve().is_relative_to(folder.resolve()):
                continue
            data = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(data.get('strokes'), list) or not data.get('stageNames'):
                continue
            legacy = data.get('rendererVersion', 0) < RENDERER_VERSION
            image = data.get('sourceName', relative.parts[0] if relative.parts else 'output')
            brush = data.get('brush', 'ellipse')
            direction = data.get('direction', 'legacy')
            stages = len(data['stageNames'])
            label = f'{image} · {BRUSHES.get(brush,brush)} · {DIRECTIONS.get(direction,"旧版方向")} · {stages} 阶段'
            if 'minDiameter' in data:
                label += f' · {data["maxDiameter"]:g}→{data["minDiameter"]:g} px'
            if data.get('preset'):
                label += ' · '+data['preset']['name']
            if data.get('settings'):
                identifier=hashlib.sha256(json.dumps(data['settings'],sort_keys=True,separators=(',',':')).encode()).hexdigest()[:6]
                label += ' · 配置 '+identifier
            if legacy:
                label += '（旧版）'
            items.append(dict(id=relative.as_posix(), label=label, image=image, brush=brush, direction=direction,
                              legacy=legacy, stages=stages, outputURL='/output/'+quote(relative.as_posix(),safe='/')+'/'))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return items


class PaintingServer(ThreadingHTTPServer):
    def __init__(self, address, root=ROOT):
        self.root = Path(root).resolve()
        for directory in ['input', 'output']:
            (self.root / directory).mkdir(parents=True, exist_ok=True)
        self.job = None
        self.lock = threading.Lock()
        self.presets = PresetStore(self.root)
        super().__init__(address, Handler)

    def start_job(self, name, brush='ellipse', direction='sobel', stages=10, min_diameter=5, settings=None, preset_id=None):
        if source_revision() != LOADED_REVISION:
            raise RuntimeError(RESTART_MESSAGE)
        input_path(name, self.root)
        settings=normalize_settings(settings if settings is not None else dict(brush=brush,direction=direction,stages=stages,minDiameter=min_diameter))
        brush,direction,stages,min_diameter=settings['brush'],settings['direction'],settings['stages'],settings['minDiameter']
        presets=self.presets.list()
        if preset_id=='custom':
            preset=dict(id='custom',name='自定义')
        elif preset_id is not None:
            selected=next((p for p in presets if p['id']==preset_id),None)
            if selected is None or selected['settings']!=settings:
                raise ValueError('预设与当前参数不一致，请重新选择预设或使用自定义')
            preset=dict(id=selected['id'],name=selected['name'])
        else:
            preset=classify_preset(settings,presets)
        validate_settings(stages, min_diameter)
        if not isinstance(brush, str) or not isinstance(direction, str) or brush not in BRUSHES or direction not in DIRECTIONS:
            raise ValueError('笔刷或方向选项无效')
        with self.lock:
            if self.job and self.job['status'] == 'running':
                raise RuntimeError('已有图片正在生成，请等待完成')
            self.job = dict(id=uuid.uuid4().hex, image=name, brush=brush, direction=direction,
                            stages=stages, minDiameter=min_diameter,settings=settings,preset=preset,status='running', message='准备生成…')
            job = dict(self.job)
        def update(message):
            with self.lock:
                self.job['message'] = message
        def work():
            try:
                output = generate(name, stages=stages, progress=update, root=self.root, brush=brush,
                                  direction=direction, min_diameter=min_diameter,settings=settings,preset=preset)
                output_id = output.relative_to(self.root / 'output').as_posix()
                with self.lock:
                    self.job.update(status='completed', outputId=output_id, outputURL='/output/'+quote(output_id, safe='/')+'/')
            except Exception as error:
                with self.lock:
                    self.job.update(status='failed', message=str(error))
        threading.Thread(target=work, daemon=True).start()
        return job


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        request, address, server = args
        super().__init__(request, address, server, directory=str(server.root), **kwargs)

    def end_headers(self):
        self.send_header('Cache-Control', 'no-store')
        super().end_headers()

    def send_json(self, value, status=200):
        payload = json.dumps(value, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == '/api/status':
            self.send_json(dict(apiVersion=API_VERSION, revision=LOADED_REVISION,
                                restartRequired=source_revision()!=LOADED_REVISION))
        elif path == '/api/images':
            self.send_json(dict(images=catalogue(self.server.root)))
        elif path == '/api/presets':
            try:
                self.send_json(dict(presets=self.server.presets.list(),defaults=DEFAULTS,choices=CHOICES,numbers=NUMBERS))
            except (ValueError,OSError) as error:
                self.send_json(dict(error=str(error)),400)
        elif path == '/api/outputs':
            self.send_json(dict(outputs=saved_outputs(self.server.root)))
        elif path == '/api/job':
            with self.server.lock:
                job = dict(self.server.job) if self.server.job else None
            self.send_json(dict(job=job))
        elif path == '/':
            self.send_response(302)
            self.send_header('Location', '/web/replay.html')
            self.end_headers()
        else:
            super().do_GET()

    def do_POST(self):
        route=urlsplit(self.path).path
        if route not in {'/api/generate','/api/presets'}:
            self.send_json(dict(error='接口不存在'), 404)
            return
        # Only same-origin local page requests may start work.
        origin = self.headers.get('Origin')
        if origin and origin != 'http://'+self.headers.get('Host', ''):
            self.send_json(dict(error='请求来源不匹配'), 403)
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 65536:
                raise ValueError('请求内容无效')
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError('请求必须为 JSON 对象')
            if route=='/api/presets':
                saved=self.server.presets.save(payload.get('name'),payload.get('settings'),payload.get('id'))
                self.send_json(dict(preset=saved));return
            job = self.server.start_job(payload.get('image'), payload.get('brush','ellipse'), payload.get('direction','sobel'),
                                        payload.get('stages',10), payload.get('minDiameter',5),payload.get('settings'),payload.get('presetId'))
            self.send_json(dict(job=job), 202)
        except (ValueError, OSError) as error:
            self.send_json(dict(error=str(error)), 400)
        except RuntimeError as error:
            self.send_json(dict(error=str(error)), 409)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, help='Fixed port; otherwise choose an available port from 8765 to 8774')
    parser.add_argument('--open', action='store_true', help='Open the replay page')
    args = parser.parse_args()
    server = None
    for port in [args.port] if args.port is not None else range(8765, 8775):
        try:
            server = PaintingServer(('127.0.0.1', port))
            break
        except OSError as error:
            if error.errno not in {errno.EADDRINUSE, 10048} or args.port is not None:
                raise
    if server is None:
        raise SystemExit('8765～8774 端口均已占用，请用 --port 指定其他端口')
    url = f'http://127.0.0.1:{server.server_port}/web/replay.html'
    print(f'Open {url}', flush=True)
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
