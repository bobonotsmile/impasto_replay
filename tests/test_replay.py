"""Exercise image selection, generation APIs, isolated outputs, and replay pixels."""
import base64
from io import BytesIO
import json
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from PIL import Image, ImageChops, ImageDraw
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from refine import generate, output_path, make_specs, validate_settings
from server import PaintingServer, catalogue


class ReplayTest(unittest.TestCase):
    def test_browser_generation_and_isolation(self):
        with tempfile.TemporaryDirectory(prefix='impasto-test-') as temp:
            root = Path(temp)
            (root / 'input').mkdir()
            shutil.copytree(ROOT / 'web', root / 'web')
            names = ['风景 测试.png', '风景 测试.jpg']
            for i, name in enumerate(names):
                image = Image.new('RGB', (96+i*32, 128-i*32), '#283d52' if i == 0 else '#c8a376')
                draw = ImageDraw.Draw(image)
                draw.ellipse((12,12,76,86), fill='#cf6c4d' if i == 0 else '#3e5340')
                draw.line((0,100,96,12), fill='white', width=3)
                image.save(root / 'input' / name)
            def result_id(name, brush='ellipse', direction='sobel', stages=10, minimum=5):
                return output_path(name, root, brush, direction, stages, minimum).relative_to(root/'output').as_posix()
            server = PaintingServer(('127.0.0.1', 0), root)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f'http://127.0.0.1:{server.server_port}'
            try:
                with sync_playwright() as p:
                    browser = p.chromium.launch(channel='msedge', headless=True)
                    try:
                        page = browser.new_page()
                        errors = []
                        page.on('pageerror', lambda error: errors.append(str(error)))
                        page.goto(base+'/web/replay.html')
                        page.wait_for_function("!document.querySelector('#imageSelect').disabled")
                        self.assertEqual(page.locator('#imageSelect option').count(), 2)
                        self.assertTrue(page.locator('#play').is_disabled())
                        for invalid in ['../source.png', 'missing.png', None]:
                            self.assertEqual(page.request.post(base+'/api/generate', data={'image':invalid}).status, 400)
                        for options in [{'stages':1},{'stages':1.5},{'stages':True},{'minDiameter':0},{'minDiameter':36},{'minDiameter':True}]:
                            self.assertEqual(page.request.post(base+'/api/generate',data=dict(image=names[0],**options)).status,400)
                        # Slider readouts must match the planned 35-to-5 schedule.
                        self.assertIn('35 → 31.67', page.locator('#sizePlan').inner_text())
                        first_data = None
                        for name in names:
                            page.select_option('#imageSelect', name)
                            page.locator('#generate').click()
                            page.wait_for_function("document.querySelector('#imageSelect').disabled")
                            page.wait_for_function("async()=>{const r=await fetch('/api/job');const {job}=await r.json();return job?.status==='running'}")
                            self.assertEqual(page.request.post(base+'/api/generate', data={'image':name}).status, 409)
                            page.wait_for_function("!document.querySelector('#generate').disabled", timeout=120000)
                            page.wait_for_function("document.querySelectorAll('#stageList button').length === 10", timeout=15000)
                            output = output_path(name, root)
                            data = json.loads((output / 'strokes.json').read_text(encoding='utf-8'))
                            self.assertEqual(data['sourceName'], name)
                            self.assertEqual(len(data['stageNames']), 10)
                            self.assertEqual(data['stageBrushDiameters'], [round(35-30*i/9,4) for i in range(10)])
                            self.assertGreaterEqual(min(s['d'] for s in data['strokes']), 5)
                            self.assertEqual({s['brush'] for s in data['strokes'] if s['stage'] <= 3}, {'round'})
                            self.assertEqual({s['brush'] for s in data['strokes'] if s['stage'] > 3}, {'ellipse'})
                            self.assertEqual(data['direction'], 'sobel')
                            self.assertEqual(len(data['strokes']), sum(data['stageStrokeCounts']))
                            self.assertEqual(len(list((output / 'stages').glob('*.png'))), 10)
                            metrics = json.loads((output / 'refinement_metrics.json').read_text())
                            for entry in metrics['stages']:
                                self.assertLessEqual(entry['lossAfter'], entry['lossBefore'] + .001)
                            self.assertEqual(metrics['stages'][-1]['detail'],1)
                            if first_data is None:
                                first_data = (output / 'strokes.json').read_bytes()
                            else:
                                self.assertEqual((output_path(names[0], root) / 'strokes.json').read_bytes(), first_data)
                            for stage in [1, 5, 10]:
                                page.locator(f'button[data-stage="{stage}"]').click()
                                url = page.locator('canvas').evaluate("c=>c.toDataURL('image/png')")
                                actual = Image.open(BytesIO(base64.b64decode(url.split(',')[1]))).convert('RGB')
                                expected = Image.open(output / f'stages/stage_{stage:02d}.png').convert('RGB')
                                self.assertIsNone(ImageChops.difference(actual, expected).getbbox(), f'{name}: stage {stage}')
                            self.assertEqual(page.request.get(base+page.locator('#previewLink').get_attribute('href')).status, 200)
                        page.locator('#reset').click()
                        self.assertTrue(page.locator('#count').inner_text().startswith('0 /'))
                        page.locator('#play').click()
                        page.wait_for_function("!document.querySelector('#count').textContent.startsWith('0 /')")
                        page.locator('#pause').click()
                        paused = page.locator('#count').inner_text()
                        page.wait_for_timeout(100)
                        self.assertEqual(paused, page.locator('#count').inner_text())
                        page.locator('#progress').evaluate("e=>{e.value='1';e.dispatchEvent(new Event('input'))}")
                        self.assertTrue(page.locator('#stage').inner_text().startswith('10 '))
                        # A refreshed page must discover saved outputs without regenerating.
                        page.reload()
                        page.wait_for_function("!document.querySelector('#imageSelect').disabled")
                        page.select_option('#outputSelect', result_id(names[0]))
                        page.wait_for_function("document.querySelector('#loadStatus').textContent.includes('已载入')")
                        self.assertEqual(page.locator('canvas').evaluate('(c)=>[c.width,c.height]'), [96,128])
                        # A second brush for the same photo has its own files and
                        # keeps its own large silhouette from the first stage.
                        page.select_option('#imageSelect', names[0])
                        previous_output = page.locator('#outputSelect').input_value()
                        page.select_option('#brushSelect', 'knife')
                        self.assertEqual(page.locator('#outputSelect').input_value(), previous_output)
                        self.assertFalse(page.locator('#play').is_disabled())
                        page.locator('#generate').click()
                        page.wait_for_function("document.querySelector('#generate').disabled")
                        page.wait_for_function("!document.querySelector('#generate').disabled", timeout=120000)
                        knife_output = output_path(names[0], root, 'knife', 'sobel')
                        knife_data = json.loads((knife_output / 'strokes.json').read_text(encoding='utf-8'))
                        self.assertEqual({s['brush'] for s in knife_data['strokes']}, {'knife'})
                        self.assertEqual(knife_data['roundStages'], 0)
                        self.assertEqual((output_path(names[0], root) / 'strokes.json').read_bytes(), first_data)
                        self.assertEqual(len(next(i for i in catalogue(root) if i['name'] == names[0])['variants']), 2)
                        page.select_option('#brushSelect', 'ellipse')
                        page.select_option('#directionSelect', 'tensor')
                        self.assertEqual(page.locator('#outputSelect').input_value(), result_id(names[0], 'knife'))
                        page.locator('#generate').click()
                        page.wait_for_function("document.querySelector('#generate').disabled")
                        page.wait_for_function("!document.querySelector('#generate').disabled", timeout=120000)
                        tensor_output = output_path(names[0], root, 'ellipse', 'tensor')
                        self.assertTrue((tensor_output / 'preview.png').exists())
                        self.assertEqual(page.locator('#outputSelect').input_value(), result_id(names[0], 'ellipse', 'tensor'))
                        self.assertEqual((output_path(names[0], root) / 'strokes.json').read_bytes(), first_data)
                        page.select_option('#outputSelect', result_id(names[0]))
                        page.select_option('#directionSelect', 'sobel')
                        page.wait_for_function("document.querySelectorAll('#stageList button').length === 10")
                        # A different grain size and iteration count is a new
                        # result; it must not overwrite the previous painting.
                        page.locator('#minDiameter').evaluate("e=>{e.value='8';e.dispatchEvent(new Event('input'))}")
                        page.locator('#iterations').evaluate("e=>{e.value='4';e.dispatchEvent(new Event('input'))}")
                        self.assertEqual(page.locator('#minDiameterValue').inner_text(),'8 px')
                        self.assertEqual(page.locator('#iterationsValue').inner_text(),'4 次')
                        page.locator('#generate').click()
                        page.wait_for_function("document.querySelector('#generate').disabled")
                        self.assertTrue(page.locator('#minDiameter').is_disabled())
                        page.wait_for_function("!document.querySelector('#generate').disabled",timeout=120000)
                        custom = output_path(names[0],root,stages=4,min_diameter=8)
                        custom_data=json.loads((custom/'strokes.json').read_text(encoding='utf-8'))
                        self.assertEqual(custom_data['stageBrushDiameters'],[35,26,17,8])
                        self.assertGreaterEqual(min(s['d'] for s in custom_data['strokes']),8)
                        self.assertEqual(len(list((custom/'stages').glob('*.png'))),4)
                        self.assertEqual(page.locator('#outputSelect').input_value(),result_id(names[0],stages=4,minimum=8))
                        self.assertEqual((output_path(names[0],root)/'strokes.json').read_bytes(),first_data)
                        # Failure leaves a previous complete result intact and unlocks the UI.
                        (root / 'input' / names[0]).write_bytes(b'broken image')
                        page.locator('#generate').click()
                        page.wait_for_function("document.querySelector('#generate').disabled")
                        page.wait_for_function("!document.querySelector('#generate').disabled", timeout=15000)
                        self.assertEqual((output_path(names[0], root) / 'strokes.json').read_bytes(), first_data)
                        self.assertEqual(server.job['status'], 'failed')
                        self.assertFalse(next(i for i in catalogue(root) if i['name'] == names[0])['hasOutput'])
                        # Saved playback does not depend on the input catalogue.
                        (root / 'input' / names[0]).unlink()
                        page.locator('#refreshImages').click()
                        page.locator('#refreshOutputs').click()
                        page.select_option('#outputSelect', result_id(names[0], 'knife'))
                        page.wait_for_function("document.querySelector('#previewLink').getAttribute('href').includes('knife-sobel')")
                        page.wait_for_function("!document.querySelector('#play').disabled")
                        self.assertFalse(page.locator('#play').is_disabled())
                        self.assertEqual(errors, [])
                    finally:
                        browser.close()
            finally:
                server.shutdown()
                server.server_close()
            # A shorter CLI run writes a separate configuration.
            generate(names[1], stages=4, root=root, progress=lambda message:None)
            self.assertFalse((output_path(names[1], root, stages=4) / 'stages/stage_10.png').exists())
            self.assertTrue((output_path(names[1], root) / 'stages/stage_10.png').exists())
            self.assertEqual((output_path(names[0], root) / 'strokes.json').read_bytes(), first_data)

    def test_all_brushes_and_empty_stages(self):
        with tempfile.TemporaryDirectory(prefix='impasto-brushes-') as temp:
            root = Path(temp)
            (root / 'input').mkdir()
            Image.new('RGB', (32,32), '#30231e').save(root / 'input/solid.png')
            # This target is already converged (blur rounding can introduce a
            # one-level difference). Empty stages must still export snapshots.
            for brush in ['round', 'flat', 'long']:
                output = generate('solid.png', stages=11, brush=brush, direction='tensor', root=root, progress=lambda message:None)
                data = json.loads((output / 'strokes.json').read_text(encoding='utf-8'))
                self.assertEqual(data['roundStages'], 0)
                self.assertIn(0, data['stageStrokeCounts'])
                self.assertEqual(len(list((output / 'stages').glob('*.png'))), 11)
                self.assertTrue({s['brush'] for s in data['strokes']}.issubset({brush}))
                for stage,count in enumerate(data['stageStrokeCounts'],1):
                    if stage>1 and count==0:
                        self.assertEqual((output/f'stages/stage_{stage:02d}.png').read_bytes(),
                                         (output/f'stages/stage_{stage-1:02d}.png').read_bytes())

    def test_direction_fields_and_brush_shapes(self):
        with sync_playwright() as p:
            browser = p.chromium.launch(channel='msedge', headless=True)
            try:
                page = browser.new_page()
                page.add_script_tag(path=str(ROOT/'web/brush.js'))
                page.add_script_tag(path=str(ROOT/'web/refine.js'))
                result = page.evaluate('''() => {
                  const horizontal=sobelGradient([0,10,20,0,10,20,0,10,20],3,3);
                  const vertical=sobelGradient([0,0,0,10,10,10,20,20,20],3,3);
                  const gx=new Float32Array(81).fill(10),gy=new Float32Array(81);
                  for(let i=0;i<81;i++)gy[i]=(i%9)%2? -5:5;
                  const tensor=structureTensorField(gx,gy,9,9,2);
                  const shapes={};
                  for(const brush of ['round','ellipse','flat','long','knife']){
                    const canvas=document.createElement('canvas');canvas.width=80;canvas.height=80;
                    const ctx=canvas.getContext('2d',{willReadFrequently:true});
                    paintStroke(ctx,{x:40,y:40,d:20,alpha:1,color:'#804020',brush,angle:0});
                    const pixels=ctx.getImageData(0,0,80,80).data;
                    let left=80,right=0,top=80,bottom=0,wrongColor=false;
                    for(let y=0;y<80;y++)for(let x=0;x<80;x++){
                      const i=(y*80+x)*4;
                      if(pixels[i+3]){left=Math.min(left,x);right=Math.max(right,x);top=Math.min(top,y);bottom=Math.max(bottom,y);}
                      if(pixels[i+3]===255&&(pixels[i]!==128||pixels[i+1]!==64||pixels[i+2]!==32))wrongColor=true;
                    }
                    shapes[brush]={width:right-left+1,height:bottom-top+1,wrongColor};
                  }
                  return {horizontal:[horizontal.gx[4],horizontal.gy[4]],vertical:[vertical.gx[4],vertical.gy[4]],
                          tensorAngle:tensor.angles[40],tensorConfidence:tensor.confidence[40],shapes};
                }''')
                self.assertEqual(result['horizontal'], [10,0])
                self.assertEqual(result['vertical'], [0,10])
                self.assertLess(abs(result['tensorAngle']-90), 10)
                self.assertGreater(result['tensorConfidence'], .1)
                self.assertEqual(result['shapes']['round']['width'],result['shapes']['round']['height'])
                self.assertGreater(result['shapes']['long']['width']/result['shapes']['long']['height'], 4)
                for shape in result['shapes'].values():
                    self.assertFalse(shape['wrongColor'])
            finally:
                browser.close()

    def test_size_schedule_and_limits(self):
        specs=make_specs(588,632,'ellipse','sobel',10,5)
        self.assertEqual(len(specs),10)
        self.assertEqual(specs[0]['d'],35)
        self.assertEqual(specs[-1]['d'],5)
        self.assertEqual([s['brush'] for s in specs[:4]],['round','round','round','ellipse'])
        self.assertEqual([s['d'] for s in make_specs(588,632,'flat','tensor',4,5)],[35,25,15,5])
        self.assertEqual([s['brush'] for s in make_specs(588,632,'ellipse','sobel',2,5)],['round','ellipse'])
        self.assertTrue(all(s['d']==35 for s in make_specs(588,632,'long','sobel',10,35)))
        for count in [31,100,500]:
            specs=make_specs(588,632,'ellipse','sobel',count,5)
            self.assertEqual(len(specs),count)
            self.assertEqual((specs[0]['d'],specs[-1]['d']),(35,5))
        for args in [(1,5),(1.5,5),(True,5),(10,0),(10,36),(10,float('nan')),(10,True)]:
            with self.assertRaises(ValueError):
                validate_settings(*args)

    def test_iterations_above_previous_limit(self):
        with tempfile.TemporaryDirectory(prefix='impasto-many-stages-') as temp:
            root=Path(temp)
            (root/'input').mkdir()
            Image.new('RGB',(32,32),'#30231e').save(root/'input/solid.png')
            shutil.copytree(ROOT/'web',root/'web')
            server=PaintingServer(('127.0.0.1',0),root)
            threading.Thread(target=server.serve_forever,daemon=True).start()
            try:
                with sync_playwright() as p:
                    browser=p.chromium.launch(channel='msedge',headless=True)
                    try:
                        page=browser.new_page()
                        page.goto(f'http://127.0.0.1:{server.server_port}/web/replay.html')
                        page.wait_for_function("!document.querySelector('#generate').disabled")
                        page.locator('#iterationsNumber').fill('150')
                        self.assertEqual(page.locator('#iterations').input_value(),'150')
                        self.assertGreaterEqual(int(page.locator('#iterations').get_attribute('max')),150)
                        self.assertLess(len(page.locator('#sizePlan').inner_text()),300)
                        page.locator('#iterationsNumber').fill('100')
                        page.locator('#minDiameter').evaluate("e=>{e.value='1';e.dispatchEvent(new Event('input'))}")
                        page.locator('#generate').click()
                        page.wait_for_function("document.querySelector('#generate').disabled")
                        page.wait_for_function("!document.querySelector('#generate').disabled",timeout=120000)
                        page.wait_for_function("document.querySelectorAll('#stageList button').length===100")
                        output=output_path('solid.png',root,stages=100,min_diameter=1)
                        data=json.loads((output/'strokes.json').read_text(encoding='utf-8'))
                        self.assertEqual(len(data['stageBrushDiameters']),100)
                        self.assertEqual(data['stageBrushDiameters'][-1],1)
                        self.assertEqual(len(list((output/'stages').glob('*.png'))),100)
                        self.assertEqual(server.job['stages'],100)
                        self.assertEqual(server.job['minDiameter'],1)
                        # A stale Python process must reject work instead of
                        # silently applying parameters from its loaded code.
                        base=f'http://127.0.0.1:{server.server_port}'
                        with patch('server.source_revision',return_value='changed-source'):
                            self.assertTrue(page.request.get(base+'/api/status').json()['restartRequired'])
                            response=page.request.post(base+'/api/generate',data={'image':'solid.png','stages':100,'minDiameter':1})
                            self.assertEqual(response.status,409)
                        # Emulate the old server which lacks the status API.
                        page.route('**/api/status',lambda route:route.fulfill(status=404,body='old server'))
                        page.reload()
                        page.wait_for_function("document.querySelector('#generationStatus').textContent.includes('旧版本')")
                        self.assertTrue(page.locator('#generate').is_disabled())
                        requests=[]
                        page.on('request',lambda request:requests.append(request.url) if request.method=='POST' else None)
                        page.evaluate("document.querySelector('#generate').onclick()")
                        self.assertEqual(requests,[])
                    finally:
                        browser.close()
            finally:
                server.shutdown()
                server.server_close()


if __name__ == '__main__':
    unittest.main()
