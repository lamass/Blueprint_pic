"""Causal market feature and fractal registry. Add a formula with @formula(name, label)."""
import numpy as np
import pandas as pd

FORMULAS = {}
DRIVERS = {}
def formula(name, label):
    def register(fn): FORMULAS[name] = (label, fn); return fn
    return register

def driver(name, label):
    def register(fn): DRIVERS[name] = (label, fn); return fn
    return register

def load_csv(source):
    d = pd.read_csv(source); d.columns = d.columns.str.strip().str.lower()
    if 'timestamp' in d:
        d['timestamp'] = pd.to_datetime(d.timestamp, errors='raise', format='mixed')
        d = d.sort_values('timestamp', kind='stable')
    if 'price' in d and not {'open','high','low','close'}.issubset(d):
        if not {'timestamp','volume'}.issubset(d): raise ValueError('Tick CSV needs timestamp, price, volume.')
        d['price'] = pd.to_numeric(d.price); d['volume'] = pd.to_numeric(d.volume)
        if (d.volume < 0).any() or not np.isfinite(d[['price','volume']]).all().all(): raise ValueError('Invalid tick values.')
        g=d.set_index('timestamp').resample('1min'); o=g.price.ohlc(); o['volume']=g.volume.sum(); d=o.dropna().reset_index()
    required=['open','high','low','close','volume']
    if not set(required).issubset(d): raise ValueError('CSV needs open, high, low, close, volume (or tick format).')
    for k in required: d[k]=pd.to_numeric(d[k], errors='raise')
    if not np.isfinite(d[required]).all().all() or (d.volume<0).any() or (d.high<d[['open','close','low']].max(axis=1)).any() or (d.low>d[['open','close']].min(axis=1)).any(): raise ValueError('Invalid OHLCV values.')
    if not 1 <= len(d) <= 1500: raise ValueError('Use 1–1500 one-minute bars per run (one NQ session).')
    if 'timestamp' in d and d.timestamp.duplicated().any(): raise ValueError('Duplicate bar timestamps.')
    return d.reset_index(drop=True)

def features(d, tick=.25):
    o,h,l,c,v=[d[k].astype(float) for k in ['open','high','low','close','volume']]
    prev=c.shift(1); r=c-prev; tr=pd.concat([h-l,(h-prev).abs(),(l-prev).abs()],axis=1).max(axis=1)
    scale=tr.shift(1).rolling(30,min_periods=1).mean().fillna(tick).clip(lower=tick)
    vr=v.shift(1).rolling(30,min_periods=1).mean().replace(0,np.nan)
    f=pd.DataFrame({'return':r/scale,'body':(c-o)/scale,'range':(h-l)/scale,
        'wick':((c.combine(o,min)-l)-(h-c.combine(o,max)))/scale,
        'volume_rel':(v/vr).fillna(1),'state':np.sign(r),'prev_state':np.sign(r.shift(1)),
        'state_transition':np.sign(r)*np.sign(r.shift(1)), 'acceleration':(r-r.shift(1))/scale})
    f['prev_distance'] = np.sqrt((f['body']-f['body'].shift(1))**2+(f['range']-f['range'].shift(1))**2+(f['wick']-f['wick'].shift(1))**2)
    for k in (10,20,30):
        mean=c.shift(1).rolling(k,min_periods=k).mean(); hi=h.shift(1).rolling(k,min_periods=k).max(); lo=l.shift(1).rolling(k,min_periods=k).min()
        f[f'd{k}']=(c-mean)/scale; f[f'position{k}']=(c-lo)/(hi-lo).clip(lower=tick)
        f[f'compression{k}']=(hi-lo)/(scale*np.sqrt(k)); f[f'lag{k}']=(c-c.shift(k))/scale
        # Distance in normalized feature space, distinct from signed price displacement.
        cols=['body','range','wick']; hist=f[cols].shift(1).rolling(k,min_periods=k).mean()
        f[f'distance{k}']=np.sqrt(((f[cols]-hist)**2).mean(axis=1,skipna=False))
        f[f'reject_high{k}']=((h>=hi)&(c<hi)).astype(float).where(hi.notna())
        f[f'reject_low{k}']=((l<=lo)&(c>lo)).astype(float).where(lo.notna())
    return f

def val(f,k): return np.nan_to_num(f[k].to_numpy(float),nan=0,posinf=0,neginf=0)
@driver('price','Candle body & wick')
def price(f): return np.tanh(val(f,'body')) + 1j*np.tanh(val(f,'wick'))
@driver('previous','Previous bar / acceleration')
def previous(f): return np.tanh(val(f,'return')) + 1j*np.tanh(val(f,'acceleration'))
@driver('state','Up / same / down transition')
def state(f): return val(f,'state')+1j*val(f,'prev_state')
@driver('volume','Relative volume / range')
def volume(f): return np.tanh(np.log(np.maximum(val(f,'volume_rel'),.01)))+1j*np.tanh(val(f,'range')-1)

def context_driver(k):
    @driver(f'context{k}',f'{k}-bar context')
    def context(f): return np.tanh(val(f,f'd{k}')/2)+1j*np.tanh(val(f,f'distance{k}'))
for k in (10,20,30): context_driver(k)

# Each function takes arrays (bars × seeds), parameter c, step, and state.
@formula('mandelbrot','zₙ₊₁ = zₙ² + cₜ')
def mandelbrot(z,c,n,s): return z*z+c
@formula('julia','zₙ₊₁ = zₙ² + c')
def julia(z,c,n,s): return z*z+(-.8+.156j)
@formula('burning','zₙ₊₁ = (|Re z| + i|Im z|)² + cₜ')
def burning(z,c,n,s): return (abs(z.real)+1j*abs(z.imag))**2+c
@formula('newton','zₙ₊₁ = zₙ − (zₙ³ − 1)/(3zₙ²)')
def newton(z,c,n,s):
    den=3*z*z; den=np.where(abs(den)<1e-8,1e-8+0j,den); return z-(z**3-1)/den
@formula('sierpinski','zₙ₊₁ = (zₙ + Vₖ)/2')
def sierpinski(z,c,n,s):
    vertices=np.array([-.9-.55j,.9-.55j,.95j]); seed=np.arange(z.shape[1])[None,:]; k=(np.where(s<0,0,np.where(s>0,1,2)) + seed+n+(np.floor((c.real+1)*3).astype(int)))%3
    return (z+vertices[k])/2

def calculate(d, selected, weights, steps=18, seeds=12):
    f=features(d); n=len(d); active=[(key,float(weights.get(key,0))) for key in DRIVERS if float(weights.get(key,0))>0]
    mixed=sum((w*DRIVERS[key][1](f) for key,w in active),np.zeros(n,complex))/max(sum(w for _,w in active),1e-9)
    # Parameter and initial seed shifts, fixed across sessions; no full-day normalization.
    c=(-.40+.12j + .65*mixed)[:,None]; theta=np.arange(seeds)*2*np.pi/seeds
    s=(val(f,'state') if weights.get('state',0)>0 else np.zeros(n))[:,None]; output={}; descriptors={}
    for key in selected:
        if key not in FORMULAS: continue
        z=(.12*np.exp(1j*theta)[None,:]+.65*mixed[:,None]); z0=z.copy()
        if key in ('mandelbrot','burning'): z=np.broadcast_to(.08*np.exp(1j*theta),(n,seeds)).copy()
        if key=='sierpinski': z=z0*.5
        alive=np.ones(z.shape,bool); paths=[z.copy()]; escape=np.full(z.shape,steps,float)
        for j in range(steps):
            with np.errstate(all='ignore'): nxt=FORMULAS[key][1](z,c,j,s)
            valid=np.isfinite(nxt)&(abs(nxt)<(50 if key=='newton' else 4))
            just=alive&~valid; escape[just]=j+1; alive &= valid
            z=np.where(alive,nxt,z); paths.append(z.copy())
        a=np.stack(paths,axis=2); bounded=a/(1+abs(a))
        output[key]={'label':FORMULAS[key][0],'paths':np.round(np.stack([bounded.real,bounded.imag],axis=-1),4).tolist()}
        descriptors[key]={'escape_mean':escape.mean(axis=1)/steps,'escape_fraction':(escape<steps).mean(axis=1),'spread':np.std(bounded[:,:,-1],axis=1),'orbit_length':np.abs(np.diff(bounded,axis=2)).sum(axis=2).mean(axis=1)}
        for k,x in descriptors[key].items(): f[f'{key}_{k}']=x
    context=np.stack([np.tanh(val(f,f'd{k}')/2) for k in (10,20,30)],axis=1)
    parts=[(.55*weights.get('previous',0),np.tanh(val(f,'return'))),(.25*weights.get('price',0),np.tanh(val(f,'body'))),(.10*weights.get('state',0),val(f,'state'))]+[(.20/3*weights.get(f'context{k}',0),context[:,j]) for j,k in enumerate((10,20,30))]
    score=sum((w*x for w,x in parts),np.zeros(n))/max(sum(w for w,_ in parts),1e-9)
    # No claim of actual buy/sell flow: price-derived directional proxy.
    intensity=np.clip(np.log1p(val(f,'volume_rel'))/np.log(4),.15,1)
    rows=f.replace([np.inf,-np.inf],np.nan).astype(object).where(f.notna(),None).to_dict('records')
    labels=[str(x) for x in d.timestamp] if 'timestamp' in d else [f'{(570+i)//60:02d}:{(570+i)%60:02d}' for i in range(n)]
    return {'bars':d[['open','high','low','close','volume']].to_numpy().tolist(),'labels':labels,'features':rows,'score':score.tolist(),'intensity':intensity.tolist(),'formulas':output,'drivers':{k:v[0] for k,v in DRIVERS.items()},'steps':steps,'seeds':seeds}