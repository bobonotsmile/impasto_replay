// Error-driven fitting at multiple image scales. Candidate color is solved over
// the actual brush footprint; acceptance compares both color and edge residuals.
function sobelGradient(values,w,h){
  const gx=new Float32Array(w*h),gy=new Float32Array(w*h);
  for(let y=0;y<h;y++)for(let x=0;x<w;x++){
    const p=y*w+x,l=Math.max(0,x-1),r=Math.min(w-1,x+1),u=Math.max(0,y-1)*w,v=Math.min(h-1,y+1)*w;
    gx[p]=(values[u+r]+2*values[y*w+r]+values[v+r]-values[u+l]-2*values[y*w+l]-values[v+l])/8;
    gy[p]=(values[v+l]+2*values[v+x]+values[v+r]-values[u+l]-2*values[u+x]-values[u+r])/8;
  }
  return {gx,gy};
}
function structureTensorField(gx,gy,w,h,radius){
  const stride=w+1,size=stride*(h+1);
  const xx=new Float64Array(size),xy=new Float64Array(size),yy=new Float64Array(size);
  for(let y=0;y<h;y++){
    let a=0,b=0,c=0;
    for(let x=0;x<w;x++){
      const p=y*w+x,q=(y+1)*stride+x+1;
      a+=gx[p]*gx[p];b+=gx[p]*gy[p];c+=gy[p]*gy[p];
      xx[q]=xx[q-stride]+a;xy[q]=xy[q-stride]+b;yy[q]=yy[q-stride]+c;
    }
  }
  const angles=new Float32Array(w*h),confidence=new Float32Array(w*h);
  for(let y=0;y<h;y++)for(let x=0;x<w;x++){
    const l=Math.max(0,x-radius),r=Math.min(w,x+radius+1),u=Math.max(0,y-radius),v=Math.min(h,y+radius+1);
    const sum=arr=>arr[v*stride+r]-arr[u*stride+r]-arr[v*stride+l]+arr[u*stride+l];
    const a=sum(xx),b=sum(xy),c=sum(yy),trace=a+c,p=y*w+x;
    angles[p]=.5*Math.atan2(2*b,a-c)*180/Math.PI+90;
    const strength=Math.sqrt(trace/((r-l)*(v-u)));
    confidence[p]=Math.sqrt((a-c)**2+4*b*b)/(trace+1e-8)*strength/(strength+8);
  }
  return {angles,confidence};
}
window.refinePainting = async function(data, sourceURL, specs, seed) {
  const w=data.width,h=data.height,N=w*h;
  const canvas=document.createElement('canvas');canvas.width=w;canvas.height=h;
  const ctx=canvas.getContext('2d',{willReadFrequently:true});
  ctx.fillStyle='#30231e';ctx.fillRect(0,0,w,h);
  for(const s of data.strokes)paintStroke(ctx,s);
  const source=new Image();source.src=sourceURL;await source.decode();
  if(source.naturalWidth!==w||source.naturalHeight!==h)throw Error('输入图片尺寸与画布不一致');
  const targetCanvas=document.createElement('canvas');targetCanvas.width=w;targetCanvas.height=h;
  const tc=targetCanvas.getContext('2d',{willReadFrequently:true});
  const patch=document.createElement('canvas'),pc=patch.getContext('2d',{willReadFrequently:true});
  let state=seed>>>0;
  function random(){state=(Math.imul(state,1664525)+1013904223)>>>0;return state/4294967296;}
  const luminance=(r,g,b)=>r*.299+g*.587+b*.114;
  const masks=new Map();
  function brushMask(d,angle,brush){
    const key=[d,angle,brush].join('/');
    if(masks.has(key))return masks.get(key);
    const radius=Math.ceil(d+3),side=radius*2+1;
    patch.width=side;patch.height=side;
    paintStroke(pc,{x:radius,y:radius,d,angle,brush,color:'#ffffff',alpha:1});
    const pixels=pc.getImageData(0,0,side,side).data;
    const mask=new Float32Array(side*side);
    for(let i=0;i<mask.length;i++)mask[i]=pixels[i*4+3]/255;
    const value={radius,side,mask};
    if(masks.size>=256)masks.delete(masks.keys().next().value);
    masks.set(key,value);return value;
  }
  const metrics=[];
  for(let specIndex=0;specIndex<specs.length;specIndex++){
    const spec=specs[specIndex],stage=data.stageNames.length+1;
    const detail=specIndex/Math.max(1,specs.length-1),edgeStrength=.15+detail*.65;
    // Keep the target opaque at canvas boundaries when applying the blur.
    tc.filter='none';tc.drawImage(source,0,0);
    tc.filter=`blur(${Math.max(0,(spec.d-1)*.25)}px)`;tc.drawImage(source,0,0);
    const target=tc.getImageData(0,0,w,h).data;
    const pixels=ctx.getImageData(0,0,w,h).data;
    const targetY=new Float32Array(N),residual=new Float32Array(N),weight=new Float32Array(N);
    for(let p=0;p<N;p++){
      const i=p*4;targetY[p]=luminance(target[i],target[i+1],target[i+2]);
      residual[p]=luminance(pixels[i]-target[i],pixels[i+1]-target[i+1],pixels[i+2]-target[i+2]);
    }
    const {gx,gy}=sobelGradient(targetY,w,h);
    const tensor=spec.direction==='tensor'?structureTensorField(gx,gy,w,h,Math.max(1,Math.min(12,Math.round(spec.d*.25)))):null;
    const coherence=new Float32Array(N);
    for(let y=0;y<h;y++)for(let x=0;x<w;x++){
      const p=y*w+x;
      weight[p]=1+Math.min(2,Math.hypot(gx[p],gy[p])/24)*detail;
      const magnitude=Math.hypot(gx[p],gy[p]);
      coherence[p]=tensor?tensor.confidence[p]:magnitude/(magnitude+8);
    }
    const error=new Float32Array(N),failures=new Uint8Array(N);
    function loss(p,r,g,b,nextY,rightY,downY){
      let value=weight[p]*(r*r+g*g+b*b+1.5*nextY*nextY);
      if(p%w<w-1)value+=edgeStrength*(nextY-rightY)**2;
      if(p<N-w)value+=edgeStrength*(nextY-downY)**2;
      return value;
    }
    function currentError(p){
      const i=p*4;
      return loss(p,pixels[i]-target[i],pixels[i+1]-target[i+1],pixels[i+2]-target[i+2],
        residual[p],residual[Math.min(N-1,p+1)],residual[Math.min(N-1,p+w)]);
    }
    const mse=()=>{
      let sum=0;for(let i=0;i<pixels.length;i+=4)for(let c=0;c<3;c++)sum+=(pixels[i+c]-target[i+c])**2;
      return sum/(N*3);
    };
    for(let p=0;p<N;p++)error[p]=currentError(p);
    const mseBefore=mse(),lossBefore=error.reduce((a,b)=>a+b,0)/(N*3);
    let accepted=0,attempts=0,candidates=0;
    while(accepted<spec.count&&attempts<spec.count*6){
      attempts++;
      // Pool neighboring errors to prefer incomplete regions over one noisy
      // pixel. A failure penalty prevents repeatedly proposing rejected marks.
      let best=0,bestScore=-1;
      const reach=Math.max(1,Math.round(spec.d*.3));
      for(let k=0;k<24;k++){
        const p=Math.floor(random()*N),x=p%w,y=Math.floor(p/w);
        let region=error[p];
        for(const [dx,dy] of [[reach,0],[-reach,0],[0,reach],[0,-reach]])
          region+=error[Math.max(0,Math.min(h-1,y+dy))*w+Math.max(0,Math.min(w-1,x+dx))];
        const center=.2+detail*.6;
        const score=(center*error[p]+(1-center)*region/5)/(1+failures[p]*2);
        if(score>bestScore){best=p;bestScore=score;}
      }
      if(bestScore<.02)break;
      const x=best%w,y=Math.floor(best/w),brush=spec.brush;
      let angle;
      angle=coherence[best]>.05?(tensor?tensor.angles[best]:Math.atan2(gy[best],gx[best])*180/Math.PI+90):45;
      angle=Math.round(angle);
      const proposals=brush==='round'?[{scale:1},{scale:.72}]:
        [{scale:1},{scale:.78},{scale:1,opacity:.7}];
      let winner=null;
      for(const proposal of proposals){
        const d=Math.max(spec.minDiameter??.75,Math.round(spec.d*proposal.scale*10000)/10000);
        const direction=brush==='round'?0:angle;
        const alpha=proposal.opacity??(detail<.15?.97:Math.round((.94-detail*.12)*100)/100);
        const {radius,side,mask}=brushMask(d,direction,brush);
        const left=Math.max(0,x-radius),top=Math.max(0,y-radius);
        const pw=Math.min(w,x+radius+1)-left,ph=Math.min(h,y+radius+1)-top;
        const original=ctx.getImageData(left,top,pw,ph);
        let denom=0,red=0,green=0,blue=0,oldLoss=0;
        for(let yy=0;yy<ph;yy++)for(let xx=0;xx<pw;xx++){
          const p=(top+yy)*w+left+xx,i=p*4;
          const a=mask[(top+yy-y+radius)*side+left+xx-x+radius]*alpha;
          const weighted=weight[p]*a;
          denom+=weighted*a;
          red+=weighted*(target[i]-(1-a)*pixels[i]);
          green+=weighted*(target[i+1]-(1-a)*pixels[i+1]);
          blue+=weighted*(target[i+2]-(1-a)*pixels[i+2]);
          oldLoss+=error[p];
        }
        if(denom<1e-8)continue;
        const rgb=[red,green,blue].map(v=>Math.max(0,Math.min(255,Math.round(v/denom))));
        const s={x,y,d,brush,angle:direction,alpha,stage,color:'#'+rgb.map(v=>v.toString(16).padStart(2,'0')).join('')};
        patch.width=pw;patch.height=ph;pc.putImageData(original,0,0);
        pc.save();pc.translate(-left,-top);paintStroke(pc,s);pc.restore();
        const next=pc.getImageData(0,0,pw,ph),nextResidual=new Float32Array(pw*ph);
        for(let yy=0;yy<ph;yy++)for(let xx=0;xx<pw;xx++){
          const p=(top+yy)*w+left+xx,q=yy*pw+xx,i=p*4,j=q*4;
          nextResidual[q]=luminance(next.data[j]-target[i],next.data[j+1]-target[i+1],next.data[j+2]-target[i+2]);
        }
        let newLoss=0;
        for(let yy=0;yy<ph;yy++)for(let xx=0;xx<pw;xx++){
          const p=(top+yy)*w+left+xx,q=yy*pw+xx,i=p*4,j=q*4;
          newLoss+=loss(p,next.data[j]-target[i],next.data[j+1]-target[i+1],next.data[j+2]-target[i+2],
            nextResidual[q],xx<pw-1?nextResidual[q+1]:residual[Math.min(N-1,p+1)],
            yy<ph-1?nextResidual[q+pw]:residual[Math.min(N-1,p+w)]);
        }
        candidates++;
        const gain=oldLoss-newLoss;
        if(gain>1e-4&&(!winner||gain>winner.gain))winner={s,gain,next,nextResidual,left,top,pw,ph};
      }
      if(!winner){failures[best]=Math.min(30,failures[best]+1);continue;}
      const {s,next,nextResidual,left,top,pw,ph}=winner;
      ctx.putImageData(next,left,top);
      for(let yy=0;yy<ph;yy++)for(let xx=0;xx<pw;xx++){
        const p=(top+yy)*w+left+xx,q=yy*pw+xx;
        for(let c=0;c<4;c++)pixels[p*4+c]=next.data[q*4+c];
        residual[p]=nextResidual[q];failures[p]=0;
      }
      // Brush support is strictly inside the patch, leaving a halo for edge
      // terms. Refresh the halo too so later proposals see the current loss.
      for(let yy=0;yy<ph;yy++)for(let xx=0;xx<pw;xx++){
        const p=(top+yy)*w+left+xx;error[p]=currentError(p);
      }
      data.strokes.push(s);accepted++;
    }
    data.stageNames.push(spec.name);
    data.stageBrushDiameters.push(spec.d);data.stageStrokeCounts.push(accepted);
    const lossAfter=error.reduce((a,b)=>a+b,0)/(N*3),mseAfter=mse();
    metrics.push({stage,diameter:spec.d,minDiameter:spec.minDiameter,detail,edgeStrength,
      blurRadius:Math.max(0,(spec.d-1)*.25),strokeBudget:spec.count,brush:spec.brush,direction:spec.direction,strokes:accepted,attempts,candidates,
      mseBefore,mseAfter,lossBefore,lossAfter});
    console.log(`stage ${stage}: ${accepted} strokes, loss ${lossBefore.toFixed(2)} -> ${lossAfter.toFixed(2)}`);
  }
  // Export a full replay pass, avoiding local-patch antialias rounding drift.
  const snapshots=[];
  ctx.fillStyle='#30231e';ctx.fillRect(0,0,w,h);
  let cursor=0;
  for(const count of data.stageStrokeCounts){
    for(let i=0;i<count;i++)paintStroke(ctx,data.strokes[cursor++]);
    snapshots.push(canvas.toDataURL('image/png'));
  }
  return {data,snapshots,metrics};
};
