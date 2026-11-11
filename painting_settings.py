"""Painting parameter schema and named presets stored in one JSON file."""
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import threading
import uuid

DEFAULTS = {
    'brush':'ellipse', 'direction':'sobel', 'minDiameter':5, 'maxDiameter':35,
    'stages':10, 'seed':20261110, 'brushStyle':'solid', 'textureStrength':.3,
    'strokeMode':'stamp', 'strokeLength':3, 'placementMode':'global', 'locality':.75,
    'pressureMode':'stage', 'pressureStrength':.7, 'toneMode':'reference', 'toneStrength':.7,
    'paintMode':'uniform', 'paintLoad':.8, 'optimizationMode':'error', 'strokeBudget':0,
    'replayMode':'single', 'replaySpeed':'4',
}
CHOICES = {
    'brush':{'ellipse':'椭圆刷','round':'圆头刷','flat':'扁平刷','long':'长条刷','knife':'刮刀'},
    'direction':{'sobel':'Sobel 边缘方向场','tensor':'Structure Tensor 局部方向场'},
    'brushStyle':{'solid':'几何笔尖','textured':'纹理笔尖'},
    'strokeMode':{'stamp':'单点印章','path':'连续曲线'},
    'placementMode':{'global':'全局误差选点','local':'局部连续绘制'},
    'pressureMode':{'stage':'阶段透明度','taper':'模拟压感与起收笔'},
    'toneMode':{'reference':'目标图原色','dark-light':'暗部→中间调→亮部'},
    'paintMode':{'uniform':'均匀覆盖','dry':'拖笔渐干'},
    'optimizationMode':{'error':'还原优先','efficient':'少笔优先'},
    'replayMode':{'batch':'批量回放','single':'每帧一笔'},
    'replaySpeed':{'1':'1× 正常','2':'2×','4':'4×','8':'8×','16':'16×'},
}
NUMBERS = {
    'minDiameter':(1,256), 'maxDiameter':(1,256), 'stages':(2,None), 'seed':(0,4294967295),
    'textureStrength':(0,1), 'strokeLength':(1,8), 'locality':(0,1),
    'pressureStrength':(0,1), 'toneStrength':(0,1), 'paintLoad':(.1,1), 'strokeBudget':(0,None),
}
INTEGER_FIELDS = {'stages','seed','strokeBudget'}


def normalize_settings(value):
    if not isinstance(value, dict):
        raise ValueError('绘画参数必须为 JSON 对象')
    unknown = set(value)-set(DEFAULTS)
    if unknown:
        raise ValueError('未知绘画参数：'+', '.join(sorted(unknown)))
    settings = dict(DEFAULTS, **value)
    for key, options in CHOICES.items():
        if not isinstance(settings[key], str) or settings[key] not in options:
            raise ValueError(f'{key} 选项无效')
    for key,(low,high) in NUMBERS.items():
        number=settings[key]
        if (type(number) not in (int,float) or not math.isfinite(number)
                or number<low or (high is not None and number>high)
                or (key in INTEGER_FIELDS and (type(number) is not int or number>9007199254740991))):
            raise ValueError(f'{key} 数值无效')
    if settings['minDiameter']>settings['maxDiameter']:
        raise ValueError('最小颗粒度不能超过起始笔刷尺寸')
    return settings


def builtin_presets():
    flow=dict(DEFAULTS, brush='round', direction='tensor', minDiameter=1, brushStyle='textured', textureStrength=.2,
              strokeMode='path', placementMode='local', pressureMode='taper', toneMode='dark-light',
              pressureStrength=.5,toneStrength=.5,paintMode='dry',paintLoad=.95,
              optimizationMode='efficient', strokeBudget=12000, replayMode='single')
    return [dict(id='classic',name='经典铺色',builtin=True,settings=dict(DEFAULTS)),
            dict(id='flow',name='流韵厚涂',builtin=True,settings=flow)]


def classify_preset(settings, presets):
    for preset in presets:
        if settings==preset['settings']:
            return dict(id=preset['id'],name=preset['name'])
    return dict(id='custom',name='自定义')


def settings_suffix(settings):
    """Keep classic directory names; distinguish all additional settings."""
    base_fields={'brush','direction','minDiameter','maxDiameter','stages'}
    if all(settings[k]==v for k,v in DEFAULTS.items() if k not in base_fields):
        return ''
    text=json.dumps(settings,sort_keys=True,separators=(',',':'))
    return '-'+hashlib.sha256(text.encode()).hexdigest()[:12]


class PresetStore:
    def __init__(self, root):
        self.path=Path(root)/'presets.json'
        self.lock=threading.Lock()

    def _read(self):
        presets=builtin_presets()
        if not self.path.exists():
            self._write(presets)
            return presets
        try:
            value=json.loads(self.path.read_text(encoding='utf-8-sig'))
            if value.get('version')!=1 or not isinstance(value.get('presets'),list):
                raise ValueError('格式或版本无效')
            ids={p['id'] for p in presets}; names={p['name'] for p in presets}
            for preset in value['presets']:
                if preset.get('id') in {'classic','flow'}:
                    continue
                if (not isinstance(preset.get('id'),str) or not preset['id'].startswith('user-')
                        or preset['id'] in ids or not isinstance(preset.get('name'),str)
                        or not 1<=len(preset['name'].strip())<=60 or preset['name'] in names):
                    raise ValueError('自定义预设名称或标识无效')
                presets.append(dict(id=preset['id'],name=preset['name'],builtin=False,
                                    settings=normalize_settings(preset['settings'])))
                ids.add(preset['id']);names.add(preset['name'])
            if value['presets']!=presets:
                self._write(presets)
            return presets
        except (ValueError,KeyError,TypeError,AttributeError) as error:
            raise ValueError(f'presets.json 读取失败：{error}；原文件已保留') from error

    def _write(self, presets):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        filename=None
        try:
            with tempfile.NamedTemporaryFile('w',encoding='utf-8',dir=self.path.parent,suffix='.tmp',delete=False) as file:
                filename=file.name
                json.dump(dict(version=1,presets=presets),file,ensure_ascii=False,indent=2)
                file.write('\n');file.flush();os.fsync(file.fileno())
            os.replace(filename,self.path)
        finally:
            if filename and Path(filename).exists():
                Path(filename).unlink()

    def list(self):
        with self.lock:
            return self._read()

    def save(self, name, settings, preset_id=None):
        if not isinstance(name,str) or not 1<=len(name.strip())<=60:
            raise ValueError('预设名称需要 1～60 个字符')
        if preset_id is not None and not isinstance(preset_id,str):
            raise ValueError('预设标识无效')
        name=name.strip();settings=normalize_settings(settings)
        with self.lock:
            presets=self._read()
            if preset_id in {'classic','flow'} or name in {'经典铺色','流韵厚涂','自定义'}:
                raise ValueError('内置预设不可覆盖，请使用新的名称')
            existing=next((p for p in presets if p['id']==preset_id),None)
            if preset_id and existing is None:
                raise ValueError('要更新的预设不存在，请另存为新预设')
            if any(p['name'].casefold()==name.casefold() and p['id']!=preset_id for p in presets):
                raise ValueError('已有同名预设，请选择该预设后更新，或使用其他名称')
            saved=dict(id=preset_id or 'user-'+uuid.uuid4().hex,name=name,builtin=False,settings=settings)
            if existing:
                presets[presets.index(existing)]=saved
            else:
                presets.append(saved)
            self._write(presets)
            return saved
