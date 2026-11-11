// Shared by the replay and the exporter so snapshots use identical brush marks.
function paintTip(ctx, s) {
  const d=s.d,a=s.alpha??.85;
  ctx.save();ctx.translate(s.x,s.y);ctx.rotate((s.angle||0)*Math.PI/180);
  ctx.globalAlpha=a;ctx.fillStyle=s.color;ctx.beginPath();
  const brush=s.brush||(d>=42?'legacy-round':'ellipse');
  if(brush==='round')ctx.arc(0,0,d*.5,0,Math.PI*2);
  else if(brush==='legacy-round')ctx.ellipse(0,0,d*.48,d*.43,0,0,Math.PI*2);
  else if(brush==='flat')ctx.rect(-d*.65,-d*.26,d*1.3,d*.52);
  else if(brush==='long')ctx.roundRect(-d*.9,-d*.14,d*1.8,d*.28,d*.06);
  else if(brush==='knife'){
    ctx.moveTo(-d*.75,-d*.24);ctx.lineTo(d*.6,-d*.15);
    ctx.lineTo(d*.75,d*.08);ctx.lineTo(-d*.5,d*.3);ctx.closePath();
  }else ctx.ellipse(0,0,d*.72,d*.32,0,0,Math.PI*2);
  ctx.fill();
  ctx.restore();
}

let strokeLayer=null;
function strokeBounds(s){
  const points=s.points?.length?s.points:[{x:0,y:0}];
  const pad=Math.ceil(s.d+3);
  const left=Math.floor(Math.min(...points.map(p=>p.x))-pad);
  const top=Math.floor(Math.min(...points.map(p=>p.y))-pad);
  return {left,top,width:Math.ceil(Math.max(...points.map(p=>p.x))+pad)-left+1,
    height:Math.ceil(Math.max(...points.map(p=>p.y))+pad)-top+1};
}

function paintStroke(ctx,s){
  if(!s.points&&s.brushStyle!=='textured'&&s.pressureMode!=='taper'&&s.paintMode!=='dry'){
    paintTip(ctx,s);return;
  }
  const bounds=strokeBounds(s);
  if(!strokeLayer)strokeLayer=document.createElement('canvas');
  strokeLayer.width=bounds.width;strokeLayer.height=bounds.height;
  const layer=strokeLayer.getContext('2d',{willReadFrequently:true});
  const points=s.points?.length?s.points:[{x:0,y:0,angle:s.angle,pressure:s.pressure??1}];
  const strength=s.pressureMode==='taper'?(s.pressureStrength??.7):0;
  for(let i=0;i<points.length;i++){
    const point=points[i],t=points.length>1?i/(points.length-1):0;
    const pressure=1-strength+strength*(point.pressure??1);
    const d=Math.max(s.minDiameter??.75,s.d*pressure);
    const load=s.paintLoad??.8;
    const coverage=s.paintMode==='dry'?(.25+.75*load)*(1-(1-load)*t*.8):1;
    paintTip(layer,{x:point.x-bounds.left,y:point.y-bounds.top,d,brush:s.brush,
      angle:point.angle??s.angle,alpha:pressure*coverage,color:'#ffffff'});
  }
  if(s.brushStyle==='textured'){
    let state=(s.textureSeed??1)>>>0;
    const random=()=>{state=(Math.imul(state,1664525)+1013904223)>>>0;return state/4294967296};
    layer.globalCompositeOperation='destination-out';
    layer.globalAlpha=(s.textureStrength??.3)*.65;
    for(const point of points){
      layer.save();layer.translate(point.x-bounds.left,point.y-bounds.top);
      layer.rotate((point.angle??s.angle??0)*Math.PI/180);
      layer.strokeStyle='#000';layer.lineWidth=Math.max(.35,s.d*.025);
      for(let k=0;k<Math.min(12,Math.max(2,Math.floor(s.d*.35)));k++){
        const y=(random()-.5)*s.d*.6;
        layer.beginPath();layer.moveTo(-s.d*.85,y);layer.lineTo(s.d*.85,y+(random()-.5)*s.d*.08);layer.stroke();
      }
      layer.restore();
    }
  }
  layer.globalAlpha=1;layer.globalCompositeOperation='source-in';
  layer.fillStyle=s.color;layer.fillRect(0,0,bounds.width,bounds.height);
  ctx.save();ctx.globalAlpha=s.alpha??.85;
  ctx.drawImage(strokeLayer,s.x+bounds.left,s.y+bounds.top);ctx.restore();
}
