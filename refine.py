"""Generate a complete coarse-to-fine brush painting for an input image."""
import argparse
import base64
import hashlib
from io import BytesIO
import json
import math
from pathlib import Path
import shutil
import tempfile

from PIL import Image, ImageDraw, ImageOps
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
EXTENSIONS = {'.png', '.jpg', '.jpeg', '.webp', '.bmp', '.tif', '.tiff'}
RENDERER_VERSION = 2
BRUSHES = {'ellipse':'椭圆刷', 'round':'圆头刷', 'flat':'扁平刷', 'long':'长条刷', 'knife':'刮刀'}
DIRECTIONS = {'sobel':'Sobel 边缘方向场', 'tensor':'Structure Tensor 局部方向场'}


def validate_settings(stages, min_diameter, max_diameter=35):
    if type(stages) is not int or stages < 2:
        raise ValueError('迭代次数必须为不小于 2 的整数')
    if (type(min_diameter) not in (int, float) or type(max_diameter) not in (int, float)
            or not math.isfinite(min_diameter) or not math.isfinite(max_diameter)
            or not 1 <= min_diameter <= max_diameter <= 256):
        raise ValueError('笔刷尺寸必须满足 1 ≤ 最小尺寸 ≤ 起始尺寸 ≤ 256 px')


def make_specs(width, height, brush, direction, stages, min_diameter, max_diameter=35):
    validate_settings(stages, min_diameter, max_diameter)
    density = max(.15, (width*height/(588*632))**.5)
    round_stages = min(3, stages-1) if brush == 'ellipse' else 0
    specs = []
    for i in range(stages):
        d = round(max_diameter+(min_diameter-max_diameter)*i/(stages-1), 4)
        if i == 0:
            d = max_diameter
        elif i == stages-1:
            d = min_diameter
        d = max(min_diameter, d)
        count = max(8, round(min(5500, 65*(98/d)**1.1)*density))
        shape = 'round' if i < round_stages else brush
        phase = '圆刷起稿' if i < round_stages else BRUSHES[brush]+('塑形' if i/(stages-1)<.6 else '细化')
        specs.append(dict(d=d, minDiameter=min_diameter, count=count, brush=shape, direction=direction,
                          name=f'{phase} {d:g} px'))
    return specs


def input_path(name, root=ROOT):
    if not isinstance(name, str) or not name or '/' in name or '\\' in name:
        raise ValueError('请选择 input 文件夹中的图片文件名')
    folder = (root / 'input').resolve()
    path = (folder / name).resolve()
    if path.parent != folder or not path.is_file() or path.suffix.lower() not in EXTENSIONS:
        raise ValueError('图片不存在或格式不支持')
    return path


def output_path(name, root=ROOT, brush='ellipse', direction='sobel', stages=10, min_diameter=5, max_diameter=35):
    # Retain the extension to keep photo.jpg and photo.png in separate folders.
    return root / 'output' / name / f'{brush}-{direction}' / f'{max_diameter:g}-{min_diameter:g}px-{stages}stages'


def generate(image='source.png', stages=10, seed=20261110, browser_path=None,
             progress=print, root=ROOT, brush='ellipse', direction='sobel', min_diameter=5, max_diameter=35):
    validate_settings(stages, min_diameter, max_diameter)
    if not isinstance(brush, str) or not isinstance(direction, str) or brush not in BRUSHES or direction not in DIRECTIONS:
        raise ValueError('笔刷或方向选项无效')
    path = input_path(image, root)
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    with Image.open(BytesIO(raw)) as opened:
        im = ImageOps.exif_transpose(opened).convert('RGBA')
    original_size = im.size
    if min(im.size) < 32:
        raise ValueError('图片宽高均需至少 32 像素')
    im.thumbnail((1024, 1024), Image.Resampling.LANCZOS)
    rgb = Image.new('RGB', im.size, 'white')
    rgb.paste(im, mask=im.getchannel('A'))
    source = BytesIO()
    rgb.save(source, format='PNG')
    width, height = rgb.size
    round_stages = min(3, stages-1) if brush=='ellipse' else 0
    data = dict(width=width, height=height, stageNames=[], stageBrushDiameters=[],
                stageStrokeCounts=[], strokes=[], sourceName=image, sourceSHA256=digest,
                rendererVersion=RENDERER_VERSION, brush=brush, direction=direction, roundStages=round_stages,
                minDiameter=min_diameter, maxDiameter=max_diameter, stageCount=stages, sizeSchedule='linear')
    specs = make_specs(width, height, brush, direction, stages, min_diameter, max_diameter)
    progress('正在从空白画布生成笔触…')
    with sync_playwright() as p:
        launch = {'headless': True}
        if browser_path:
            launch['executable_path'] = browser_path
        else:
            launch['channel'] = 'msedge'
        browser = p.chromium.launch(**launch)
        try:
            page = browser.new_page()
            page.on('console', lambda msg: progress(msg.text))
            page.add_script_tag(path=str(ROOT / 'web/brush.js'))
            page.add_script_tag(path=str(ROOT / 'web/refine.js'))
            source_url = 'data:image/png;base64,'+base64.b64encode(source.getvalue()).decode()
            result = page.evaluate('(args)=>refinePainting(...args)', [data, source_url, specs, seed])
        finally:
            browser.close()
    output = output_path(image, root, brush, direction, stages, min_diameter, max_diameter)
    output.parent.mkdir(parents=True, exist_ok=True)
    progress('正在保存阶段图片与笔触数据…')
    with tempfile.TemporaryDirectory(prefix='.painting-', dir=output.parent) as temp:
        dest = Path(temp)
        (dest / 'stages').mkdir()
        for index, url in enumerate(result['snapshots'], 1):
            (dest / 'stages' / f'stage_{index:02d}.png').write_bytes(base64.b64decode(url.split(',', 1)[1]))
        shutil.copyfile(dest / 'stages' / f'stage_{stages:02d}.png', dest / 'preview.png')
        sheet = Image.new('RGB', (1100, 274*((stages+4)//5)), '#211f1c')
        draw = ImageDraw.Draw(sheet)
        for i, (d, count) in enumerate(zip(result['data']['stageBrushDiameters'], result['data']['stageStrokeCounts'])):
            with Image.open(dest / 'stages' / f'stage_{i+1:02d}.png') as snapshot:
                snapshot.thumbnail((208, 232))
                x, y = (i%5)*220+6, (i//5)*274+4
                sheet.paste(snapshot, (x,y))
            draw.text((x,y+238), f'{i+1:02d} | {d:g}px | {count} strokes', fill='white')
        sheet.save(dest / 'stage_contact_sheet.png')
        (dest / 'strokes.json').write_text(json.dumps(result['data'], ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
        metadata = dict(seed=seed, sourceName=image, sourceSHA256=digest,
                        originalSize=original_size, canvasSize=[width,height], stageCount=stages,
                        rendererVersion=RENDERER_VERSION, brush=brush, direction=direction, roundStages=round_stages,
                        minDiameter=min_diameter, maxDiameter=max_diameter, sizeSchedule='linear')
        (dest / 'refinement_metrics.json').write_text(json.dumps(dict(metadata, stages=result['metrics']), indent=2), encoding='utf-8')
        (dest / 'generation.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8')
        # Keep the previous result intact until all new artifacts are complete.
        previous = None
        if output.exists():
            previous = Path(tempfile.mkdtemp(prefix='.previous-', dir=output.parent)) / image
            output.rename(previous)
        try:
            dest.rename(output)
        except Exception:
            if previous is not None:
                previous.rename(output)
            raise
        finally:
            if previous is not None and previous.exists():
                shutil.rmtree(previous.parent)
    progress(f'完成：{stages} 阶段，{len(result["data"]["strokes"])} 笔')
    return output


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', default='source.png', help='Filename inside input/')
    parser.add_argument('--stages', type=int, default=10, metavar='N', help='Number of stages, at least 2 (no fixed upper limit)')
    parser.add_argument('--min-diameter', type=float, default=5, help='Minimum brush size in pixels (default: 5)')
    parser.add_argument('--max-diameter', type=float, default=35, help='Starting brush size in pixels (default: 35)')
    parser.add_argument('--seed', type=int, default=20261110)
    parser.add_argument('--browser', help='Optional Chromium/Edge executable path')
    parser.add_argument('--brush', choices=BRUSHES, default='ellipse')
    parser.add_argument('--direction', choices=DIRECTIONS, default='sobel')
    args = parser.parse_args()
    print(generate(args.image, args.stages, args.seed, args.browser, brush=args.brush, direction=args.direction,
                   min_diameter=args.min_diameter, max_diameter=args.max_diameter))
