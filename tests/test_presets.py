"""Preset persistence, parameter binding, and continuous-stroke replay."""
import base64
from io import BytesIO
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

from PIL import Image, ImageChops, ImageDraw
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from painting_settings import PresetStore,DEFAULTS,builtin_presets,normalize_settings,settings_suffix
from refine import make_specs,output_path
from server import PaintingServer


class PresetTest(unittest.TestCase):
    def test_playback_speed_and_generation_independence(self):
        with tempfile.TemporaryDirectory(prefix='impasto-playback-') as temp:
            root=Path(temp);(root/'input').mkdir();shutil.copytree(ROOT/'web',root/'web')
            Image.new('RGB',(64,64),'#30231e').save(root/'input/sample.png')
            settings=normalize_settings(dict(stages=2,minDiameter=1,maxDiameter=2,brush='round'))
            output=root/'output/sample.png/speed-test';output.mkdir(parents=True)
            strokes=[dict(x=i%64,y=(i//64)%64,d=1,color='#607080',alpha=.8,angle=0,brush='round',stage=1 if i<500 else 2) for i in range(1000)]
            data=dict(width=64,height=64,strokes=strokes,stageNames=['起稿','细化'],stageBrushDiameters=[2,1],
                      stageStrokeCounts=[500,500],minDiameter=1,maxDiameter=2,settings=settings)
            (output/'strokes.json').write_text(json.dumps(data),encoding='utf-8')
            server=PaintingServer(('127.0.0.1',0),root)
            threading.Thread(target=server.serve_forever,daemon=True).start()
            try:
                with sync_playwright() as p:
                    browser=p.chromium.launch(channel='msedge',headless=True)
                    try:
                        page=browser.new_page()
                        page.goto(f'http://127.0.0.1:{server.server_port}/web/replay.html')
                        page.wait_for_function("!document.querySelector('#play').disabled")
                        counts=page.evaluate('''() => {
                          function measure(mode,speed,steps){
                            pause();drawUntil(0);$('replayMode').value=mode;$('speed').value=String(speed);playing=true;
                            tick(0);cancelAnimationFrame(raf);
                            for(const t of steps){tick(t);cancelAnimationFrame(raf)}
                            const count=index;pause();return count;
                          }
                          return [measure('single',1,[1000/60]),measure('single',4,[1000/60]),
                            measure('single',16,[1000/60]),measure('batch',1,[1000/60]),
                            measure('single',1,[10,20,30,40,50,60,70,80,90,100]),
                            measure('single',1,[20,40,60,80,100])];
                        }''')
                        self.assertEqual(counts,[2,5,17,6,7,7])
                        with server.lock:
                            server.job=dict(id='test-job',image='sample.png',brush='round',direction='sobel',stages=2,minDiameter=1,
                                settings=settings,preset={'id':'custom','name':'自定义'},status='completed',message='完成',outputId='sample.png/speed-test')
                        page.evaluate("setBusy(true)")
                        self.assertFalse(page.locator('#speed').is_disabled())
                        self.assertFalse(page.locator('#replayMode').is_disabled())
                        page.select_option('#speed','16')
                        page.select_option('#replayMode','single')
                        page.evaluate('async()=>{await pollJob()}')
                        self.assertEqual(page.locator('#speed').input_value(),'16')
                        self.assertEqual(page.locator('#replayMode').input_value(),'single')
                    finally:
                        browser.close()
            finally:
                server.shutdown();server.server_close()

    def test_store_roundtrip_and_validation(self):
        with tempfile.TemporaryDirectory(prefix='impasto-presets-') as temp:
            store=PresetStore(Path(temp))
            self.assertEqual([p['name'] for p in store.list()],['经典铺色','流韵厚涂'])
            settings=normalize_settings(dict(DEFAULTS,strokeMode='path',strokeBudget=80))
            saved=store.save('我的画法',settings)
            self.assertEqual(PresetStore(Path(temp)).list()[-1],saved)
            updated=store.save('我的画法',dict(settings,pressureMode='taper'),saved['id'])
            self.assertEqual(len(store.list()),3)
            self.assertEqual(updated['id'],saved['id'])
            self.assertEqual(store.list()[0]['settings'],DEFAULTS)
            before=store.path.read_bytes()
            for name,value,preset_id in [('经典铺色',settings,None),('其他',settings,'classic'),
                    ('坏参数',dict(settings,seed=-1),None),('我的画法',settings,None)]:
                with self.assertRaises(ValueError):
                    store.save(name,value,preset_id)
                self.assertEqual(store.path.read_bytes(),before)
            store.path.write_text('{ broken',encoding='utf-8')
            with self.assertRaises(ValueError):
                store.list()
            self.assertEqual(store.path.read_text(encoding='utf-8'),'{ broken')
        self.assertEqual(settings_suffix(DEFAULTS),'')
        self.assertNotEqual(settings_suffix(settings),settings_suffix(dict(settings,strokeLength=4)))

    def test_classic_matches_previous_engine(self):
        # Compare the actual old renderer and optimizer from the committed
        # baseline against classic settings, including all sampled strokes.
        try:
            old_brush=subprocess.run(['git','show','2c98dcd:web/brush.js'],cwd=ROOT,capture_output=True,text=True,encoding='utf-8',check=True).stdout
            old_engine=subprocess.run(['git','show','2c98dcd:web/refine.js'],cwd=ROOT,capture_output=True,text=True,encoding='utf-8',check=True).stdout
        except (OSError,subprocess.CalledProcessError):
            self.skipTest('历史基准提交不可用；浅克隆或 ZIP 下载可跳过此项')
        im=Image.new('RGB',(48,48),'#354b64');ImageDraw.Draw(im).ellipse((10,6,40,44),fill='#c97d53')
        encoded=BytesIO();im.save(encoded,format='PNG')
        source='data:image/png;base64,'+base64.b64encode(encoded.getvalue()).decode()
        data=dict(width=48,height=48,stageNames=[],stageBrushDiameters=[],stageStrokeCounts=[],strokes=[],settings=DEFAULTS)
        specs=make_specs(48,48,'ellipse','sobel',4,5,16)
        with sync_playwright() as p:
            browser=p.chromium.launch(channel='msedge',headless=True)
            try:
                results=[]
                for brush,engine in [(old_brush,old_engine),((ROOT/'web/brush.js').read_text(encoding='utf-8'),(ROOT/'web/refine.js').read_text(encoding='utf-8'))]:
                    page=browser.new_page();page.add_script_tag(content=brush);page.add_script_tag(content=engine)
                    results.append(page.evaluate('(a)=>refinePainting(...a)',[data,source,specs,20261110]));page.close()
                self.assertEqual(results[0]['data']['strokes'],results[1]['data']['strokes'])
                self.assertEqual(results[0]['snapshots'],results[1]['snapshots'])
            finally:
                browser.close()

    def test_ui_save_reload_and_flow_generation(self):
        with tempfile.TemporaryDirectory(prefix='impasto-preset-ui-') as temp:
            root=Path(temp);(root/'input').mkdir();shutil.copytree(ROOT/'web',root/'web')
            im=Image.new('RGB',(80,80),'#354b64');draw=ImageDraw.Draw(im)
            draw.ellipse((12,10,68,72),fill='#ba7955');draw.line((0,72,72,5),fill='#d9caae',width=3)
            im.save(root/'input/sample.png')
            server=PaintingServer(('127.0.0.1',0),root)
            threading.Thread(target=server.serve_forever,daemon=True).start()
            try:
                with sync_playwright() as p:
                    browser=p.chromium.launch(channel='msedge',headless=True)
                    try:
                        page=browser.new_page();errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
                        page.goto(f'http://127.0.0.1:{server.server_port}/web/replay.html')
                        page.wait_for_function("!document.querySelector('#generate').disabled")
                        self.assertEqual(page.locator('#paintPreset option').count(),3)
                        page.select_option('#paintPreset','flow')
                        self.assertEqual(page.locator('#brushSelect').input_value(),'round')
                        self.assertEqual(page.locator('#minDiameter').input_value(),'1')
                        self.assertEqual(page.locator('#setting_strokeBudget').input_value(),'12000')
                        self.assertEqual(page.locator('#setting_strokeMode').input_value(),'path')
                        self.assertEqual(page.locator('#replayMode').input_value(),'single')
                        page.select_option('#brushSelect','flat')
                        page.locator('#minDiameter').evaluate("e=>{e.value='5';e.dispatchEvent(new Event('input'))}")
                        page.locator('.advanced summary').click()
                        page.locator('#setting_maxDiameter').fill('16')
                        page.locator('#iterationsNumber').fill('3')
                        page.locator('#setting_seed').fill('123')
                        page.locator('#setting_strokeBudget').fill('60')
                        self.assertEqual(page.locator('#paintPreset').input_value(),'custom')
                        page.locator('#presetName').fill('流韵小样')
                        page.locator('#savePreset').click()
                        page.wait_for_function("document.querySelector('#paintPreset').value.startsWith('user-')")
                        preset_id=page.locator('#paintPreset').input_value()
                        saved=server.presets.list()[-1]
                        self.assertEqual(saved['settings']['seed'],123)
                        page.reload();page.wait_for_function("!document.querySelector('#generate').disabled")
                        page.select_option('#paintPreset',preset_id)
                        self.assertEqual(page.locator('#iterationsNumber').input_value(),'3')
                        self.assertEqual(page.locator('#setting_seed').input_value(),'123')
                        self.assertEqual(page.locator('#setting_strokeBudget').input_value(),'60')
                        page.locator('.advanced summary').click()
                        page.locator('#setting_pressureStrength').evaluate("e=>{e.value='.6';e.dispatchEvent(new Event('input'))}")
                        page.locator('#savePreset').click()
                        page.wait_for_function("document.querySelector('#paintPreset').value.startsWith('user-')")
                        self.assertEqual(page.locator('#paintPreset').input_value(),preset_id)
                        self.assertEqual(len(server.presets.list()),3)
                        saved=server.presets.list()[-1]
                        self.assertEqual(saved['settings']['pressureStrength'],.6)
                        page.locator('#presetName').fill('流韵小样副本')
                        page.locator('#savePreset').click()
                        page.wait_for_function("document.querySelector('#presetStatus').textContent.includes('流韵小样副本')")
                        self.assertEqual(len(server.presets.list()),4)
                        self.assertTrue(any(p['id']==preset_id for p in server.presets.list()))
                        page.select_option('#paintPreset','classic')
                        self.assertEqual(page.locator('#setting_strokeMode').input_value(),'stamp')
                        self.assertEqual(page.locator('#setting_strokeBudget').input_value(),'0')
                        self.assertEqual(page.locator('#replayMode').input_value(),'single')
                        page.select_option('#paintPreset',preset_id)
                        page.locator('#generate').click()
                        page.wait_for_function("document.querySelector('#generate').disabled")
                        page.wait_for_function("!document.querySelector('#generate').disabled",timeout=120000)
                        page.wait_for_function("document.querySelectorAll('#stageList button').length===3")
                        settings=saved['settings']
                        output=output_path('sample.png',root,'flat','tensor',3,5,16,settings)
                        data=json.loads((output/'strokes.json').read_text(encoding='utf-8'))
                        self.assertEqual(data['settings'],settings)
                        self.assertEqual(data['preset']['id'],preset_id)
                        self.assertTrue(data['codeRevision'])
                        self.assertLessEqual(len(data['strokes']),60)
                        self.assertTrue(any(len(s.get('points',[]))>2 for s in data['strokes']))
                        self.assertTrue(all(s['d']>=5 for s in data['strokes']))
                        page.locator('button[data-stage="3"]').click()
                        url=page.locator('canvas').evaluate("c=>c.toDataURL('image/png')")
                        actual=Image.open(BytesIO(base64.b64decode(url.split(',')[1]))).convert('RGB')
                        expected=Image.open(output/'preview.png').convert('RGB')
                        self.assertIsNone(ImageChops.difference(actual,expected).getbbox())
                        page.locator('#reset').click()
                        self.assertEqual(page.evaluate('()=>{playing=true;tick(0);pause();return index}'),1)
                        self.assertEqual(page.evaluate('()=>{playing=true;tick(0);pause();return index}'),2)
                        self.assertEqual(errors,[])
                        page.set_viewport_size({'width':390,'height':844})
                        self.assertFalse(page.evaluate('document.documentElement.scrollWidth>innerWidth'))
                    finally:
                        browser.close()
            finally:
                server.shutdown();server.server_close()


if __name__=='__main__':
    unittest.main()
