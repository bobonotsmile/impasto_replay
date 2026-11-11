// Shared by the replay and the exporter so snapshots use identical brush marks.
function paintStroke(ctx, s) {
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
