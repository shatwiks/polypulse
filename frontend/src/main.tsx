import React, { useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { Activity, ArrowDownRight, ArrowUpRight, BarChart3, BookOpen, ChevronDown, CircleHelp, Command, Database, Gauge, LayoutDashboard, ListFilter, Radio, Search, ShieldCheck, Wallet, X } from 'lucide-react'
import './style.css'

type Book = {venue:string;market_id:string;bids:number[][];asks:number[][];best_bid:number|null;best_ask:number|null;buy_vwap_10:number|null;imbalance:number;sequence:number;valid?:boolean;stale?:boolean;recovery_status?:string;last_received_timestamp?:string|null}
type Health = {mode:string;feeds:Record<string,string>;feed_details?:Record<string,{status:string;detail?:string|null;last_received_timestamp?:string|null}>;storage:{clickhouse:string;insert_latency_ms?:number};uptime_seconds:number;ingestion_lag_seconds?:number|null;rates?:{events_received_per_second:number;events_processed_per_second:number;orderbook_processing_latency_ms_p95?:number|null};journal:{events:number;pending_export:number;snapshots:number};counters:{events:number;gaps:number;recoveries:number;duplicates:number;reconnects?:number}}
type Spread = {
  pair_id?:string;
  name:string;
  canonical_event?:string;
  canonical_outcome?:string;
  polymarket_asset_id?:string;
  kalshi_ticker?:string;
  polymarket:Book;
  kalshi:Book;
  buy_polymarket_sell_kalshi:number|null;
  buy_kalshi_sell_polymarket:number|null;
  best_spread?:number|null;
  best_spread_bps?:number|null;
  net_spread?:number|null;
  poly_fee?:number;
  kalshi_fee?:number;
  executable_qty?:number;
  executable_notional?:number;
  opportunity_type?:string;
  status?:string;
  is_arbitrage?:boolean;
  equivalence_mapping?:'direct'|'inverse';
  resolution_source?:string;
  resolution_criteria?:string;
  settlement_details?:string;
  expiration?:string;
  reviewer?:string;
  verified_at?:string|null;
  quote_age_seconds?:{polymarket:number;kalshi:number;venue_delta:number};
  vwap_by_size?:Record<string,{fillable:boolean;buy_vwap?:number;sell_vwap?:number;vwap_spread?:number;max_available?:number;poly_ask_vwap?:number;kalshi_ask_vwap?:number;poly_bid_vwap?:number;kalshi_bid_vwap?:number}>;
  note:string
}
type CrossVenueAnalyticsData = {
  source:string;
  window:string;
  min_observations:number;
  top_spread_pairs:{pair_id:string;canonical_event:string;canonical_outcome:string;equivalence_mapping:string;total_observations:number;avg_gross_spread:number;avg_gross_spread_bps:number;avg_net_spread:number;avg_net_spread_bps:number;max_gross_spread:number;max_net_spread:number;avg_executable_qty:number;executable_observations:number;executable_rate_pct:number}[];
  opportunity_frequency:{pair_id:string;canonical_event:string;total_observations:number;gross_positive_count:number;executable_count:number;fee_eroded_count:number;stale_rejected_count:number;fee_erosion_pct:number;stale_rejection_pct:number}[];
  duration_stats:{total_opportunities:number;avg_duration_seconds:number|null;median_duration_seconds:number|null;p90_duration_seconds:number|null;max_duration_seconds:number|null;avg_net_spread:number|null;avg_available_qty:number|null};
  venue_comparison:{buy_venue:string;sell_venue:string;opportunity_count:number;avg_gross_spread:number;avg_net_spread:number;avg_available_qty:number;executable_count:number}[];
  quote_freshness:{sample_count:number;avg_quote_age_poly:number|null;p95_quote_age_poly:number|null;avg_quote_age_kalshi:number|null;p95_quote_age_kalshi:number|null;avg_venue_delta_seconds:number|null;max_venue_delta_seconds:number|null;desync_count:number}
}
type ContractPair = {polymarket_asset_id:string;kalshi_ticker:string;canonical_event?:string;canonical_outcome?:string;event_description:string;outcome_equivalence:string;equivalence_mapping?:'direct'|'inverse';resolution_source:string;resolution_criteria?:string;expiration:string;polymarket_expiration?:string;kalshi_expiration?:string;settlement_details?:string;verification_status:'unverified'|'verified'|'rejected'|'incompatible'|'expired';reviewer?:string;verified_at?:string|null}
const emptyContract:ContractPair={polymarket_asset_id:'',kalshi_ticker:'',canonical_event:'',canonical_outcome:'',event_description:'',outcome_equivalence:'',equivalence_mapping:'direct',resolution_source:'',resolution_criteria:'',expiration:'',settlement_details:'',verification_status:'unverified',reviewer:''}
type Trade = {trade_id:string;wallet:string|null;venue:string;market_id:string;side:string;price:number;quantity:number|null;executed_at:number}
type WalletRow = {wallet:string;trade_count:number;active_markets?:number;maker_count?:number;taker_count?:number}
type Markout = {wallet:string;summary:{horizon_seconds:number;trade_count:number;available_count:number;average_markout:number|null;average_markout_bps?:number|null;positive_percentage:number|null;status_counts?:Record<string,number>}[];entries:{trade_id:string;horizon_seconds:number;future_mid:number|null;markout:number|null;markout_bps?:number|null;available:boolean;status?:string;side?:string;exec_price?:number}[]}
type WalletProfile = {wallet_address:string;trade_count:number;market_count:number;venue_count:number;total_volume:number;total_notional:number;first_seen:string|null;last_seen:string|null;maker_count:number;taker_count:number}
type WalletRanking = {wallet:string;trade_count:number;total_trades?:number;available_count:number;unavailable_count?:number;stale_count?:number;crossed_count?:number;horizon_seconds:number;avg_markout:number|null;avg_markout_bps:number|null;positive_percentage:number|null;qualified:boolean;sample_status:string;maker_count?:number;taker_count?:number}
type MarkoutDistribution = {horizon_seconds:number;total_count:number;available_count:number;quantiles:{p10:number|null;p25:number|null;median:number|null;p75:number|null;p90:number|null};min_bps:number|null;max_bps:number|null;avg_bps:number|null;win_rate:number|null}
type WalletTradeEntry = {trade_id:string;event_id?:string;wallet_address:string;venue:string;market_id:string;attribution_type:string;side:string;price:string;quantity:string;notional:string;exchange_timestamp?:string;received_timestamp:string;source:string}
type MarketSummary = {market_id:string;venue:string|null;window:string;trade_count:number;total_volume:number;buy_volume:number;sell_volume:number;dollar_volume:number;vwap:number|null;high_price:number|null;low_price:number|null;close_price:number|null;best_bid:number|null;best_ask:number|null;spread:number|null;spread_bps:number|null;bid_depth:number;ask_depth:number;snapshot_count:number}
type Candle = {time:string;open:number;high:number;low:number;close:number;volume:number;buy_volume:number;sell_volume:number;trade_count:number;avg_price:number}
type SpreadPoint = {time:string;avg_spread:number|null;min_spread:number|null;max_spread:number|null;avg_bid_depth:number|null;avg_ask_depth:number|null}
type StorageStats = {status:string;active_table_count:number;total_compressed_bytes:number;total_uncompressed_bytes:number;compression_ratio:number;total_rows:number;tables:{table:string;rows:number;compressed_bytes:number;uncompressed_bytes:number;compression_ratio:number}[]}
type Page = 'overview'|'orderbook'|'scanner'|'wallets'|'trades'|'analytics'|'health'

const nav: {id:Page;label:string;icon:React.ComponentType<{size?:number}>;shortcut:string}[] = [
  {id:'overview',label:'Overview',icon:LayoutDashboard,shortcut:'1'},
  {id:'orderbook',label:'Live orderbook',icon:BookOpen,shortcut:'2'},
  {id:'scanner',label:'Cross-venue scanner',icon:BarChart3,shortcut:'3'},
  {id:'wallets',label:'Wallet intelligence',icon:Wallet,shortcut:'4'},
  {id:'trades',label:'Trade explorer',icon:ListFilter,shortcut:'5'},
  {id:'analytics',label:'ClickHouse analytics',icon:Database,shortcut:'6'},
  {id:'health',label:'System health',icon:Gauge,shortcut:'7'}
]
const api = async <T,>(path:string):Promise<T> => {const r=await fetch(`/api/v1${path}`);if(!r.ok) throw new Error(`${r.status} ${r.statusText}`);return r.json()}
const price = (n:number|null|undefined) => n==null?'—':n.toFixed(4)
const signed = (n:number|null|undefined) => n==null?'—':`${n>=0?'+':''}${(n*100).toFixed(2)}¢`
const signedBps = (n:number|null|undefined) => n==null?'—':`${n>=0?'+':''}${n.toFixed(1)} bps`
const signedDollars = (n:number|null|undefined) => n==null?'—':`${n>=0?'+':''}$${Math.abs(n).toFixed(4)}`
const short = (s:string) => s.length>25?`${s.slice(0,13)}…${s.slice(-7)}`:s
const time = (n:number) => new Date(n*1000).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'})
const venueName=(s:string)=>s==='polymarket'?'Polymarket':s==='kalshi'?'Kalshi':'Polygon'
const lastEvent=(value:string|null|undefined)=>value?new Date(value).toLocaleTimeString():'Never'

function App(){
  const [page,setPage]=useState<Page>('overview')
  const [books,setBooks]=useState<Book[]>([])
  const [health,setHealth]=useState<Health|null>(null)
  const [mode,setMode]=useState<'demo'|'live'|null>(null)
  const [spreads,setSpreads]=useState<Spread[]>([])
  const [contracts,setContracts]=useState<ContractPair[]>([])
  const [contractForm,setContractForm]=useState<ContractPair>(emptyContract)
  const [contractError,setContractError]=useState('')
  const [trades,setTrades]=useState<Trade[]>([])
  const [wallets,setWallets]=useState<WalletRow[]>([])
  const [wallet,setWallet]=useState('')
  const [walletProfile,setWalletProfile]=useState<WalletProfile|null>(null)
  const [walletRankings,setWalletRankings]=useState<WalletRanking[]>([])
  const [walletTrades,setWalletTrades]=useState<WalletTradeEntry[]>([])
  const [markoutDist,setMarkoutDist]=useState<MarkoutDistribution|null>(null)
  const [rankingHorizon,setRankingHorizon]=useState<5|30|60>(30)
  const [minSampleFilter,setMinSampleFilter]=useState<number>(5)
  const [walletSearch,setWalletSearch]=useState<string>('')
  const [markout,setMarkout]=useState<Markout|null>(null)
  const [anWin,setAnWin]=useState<'1h'|'6h'|'24h'|'7d'>('24h')
  const [anRes,setAnRes]=useState<'1m'|'5m'|'1h'|'1d'>('1m')
  const [anSummary,setAnSummary]=useState<MarketSummary|null>(null)
  const [anCandles,setAnCandles]=useState<Candle[]>([])
  const [anSpread,setAnSpread]=useState<SpreadPoint[]>([])
  const [storageStats,setStorageStats]=useState<StorageStats|null>(null)
  const [selectedSpreadPair,setSelectedSpreadPair]=useState<string>('')
  const [scannerSubView,setScannerSubView]=useState<'scanner'|'analytics'|'registry'>('scanner')
  const [spreadStatusFilter,setSpreadStatusFilter]=useState<string>('all')
  const [crossVenueAnalytics,setCrossVenueAnalytics]=useState<CrossVenueAnalyticsData|null>(null)
  const [anLoading,setAnLoading]=useState(false)
  const [selected,setSelected]=useState('')
  const [search,setSearch]=useState('')
  const [commandOpen,setCommandOpen]=useState(false)
  const [socket,setSocket]=useState<'connecting'|'connected'|'disconnected'>('connecting')
  const [error,setError]=useState('')
  const [clock,setClock]=useState(new Date())
  const active=books.find(b=>`${b.venue}:${b.market_id}`===selected)||books[0]
  const bookStale=(b:Book)=>Boolean(b.stale||(b.last_received_timestamp&&clock.getTime()-new Date(b.last_received_timestamp).getTime()>=30000))
  const bookReliable=(b:Book)=>b.valid!==false&&!bookStale(b)
  const filtered=books.filter(b=>`${b.venue} ${b.market_id}`.toLowerCase().includes(search.toLowerCase()))
  const sortedSpreads=[...spreads].sort((a,b)=>Math.max(b.buy_polymarket_sell_kalshi??-2,b.buy_kalshi_sell_polymarket??-2)-Math.max(a.buy_polymarket_sell_kalshi??-2,a.buy_kalshi_sell_polymarket??-2))
  const totalMarkets=books.length
  const validBooks=books.filter(b=>bookReliable(b)&&b.best_ask!=null&&b.best_bid!=null)
  const avgSpread=validBooks.length?validBooks.reduce((a,b)=>a+(b.best_ask!-b.best_bid!),0)/validBooks.length:null

  useEffect(()=>{
    let alive=true;let ws:WebSocket|null=null;let retry:ReturnType<typeof setTimeout>;const worker=new Worker(new URL('./stream.worker.ts',import.meta.url),{type:'module'})
    worker.onmessage=(event:MessageEvent<{type:string;data:any}>)=>{
      const m=event.data
      if(m.type==='initial'){setMode(m.data.mode);setHealth(null);setBooks(m.data.markets);setSelected((m.data.markets[0])?`${m.data.markets[0].venue}:${m.data.markets[0].market_id}`:'');api<Health>('/health').then(setHealth).catch(()=>{})}
      if(m.type==='book')setBooks(prev=>{const key=(b:Book)=>`${b.venue}:${b.market_id}`;return [...prev.filter(b=>key(b)!==key(m.data)),m.data].sort((a,b)=>key(a).localeCompare(key(b)))})
      if(m.type==='health')setHealth(m.data)
    }
    const connect=()=>{if(!alive)return;setSocket('connecting');ws=new WebSocket(`${location.protocol==='https:'?'wss':'ws'}://${location.host}/ws/markets`);ws.onopen=()=>setSocket('connected');ws.onmessage=e=>worker.postMessage(e.data);ws.onclose=()=>{setSocket('disconnected');if(alive)retry=setTimeout(connect,2000)}}
    connect()
    api<Health>('/health').then(setHealth).catch(e=>setError(e.message))
    return()=>{alive=false;clearTimeout(retry);ws?.close();worker.terminate()}
  },[])
  useEffect(()=>{const id=setInterval(()=>setClock(new Date()),1000);return()=>clearInterval(id)},[])
  useEffect(()=>{const load=()=>{api<Spread[]>('/spreads').then(setSpreads).catch(()=>{});api<ContractPair[]>('/contracts').then(setContracts).catch(()=>{});api<Trade[]>('/trades').then(setTrades).catch(()=>{});api<WalletRow[]>('/wallets').then(setWallets).catch(()=>{});api<Health>('/health').then(setHealth).catch(()=>{})};load();const id=setInterval(load,5000);return()=>clearInterval(id)},[])
  const saveContract=async(e:React.FormEvent)=>{e.preventDefault();setContractError('');try{const body={...contractForm,expiration:new Date(contractForm.expiration+'Z').toISOString()};const response=await fetch('/api/v1/contracts',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});if(!response.ok)throw new Error(`Save failed (${response.status})`);setContracts(await api<ContractPair[]>('/contracts'));setContractForm(emptyContract);setSpreads(await api<Spread[]>('/spreads'))}catch(err){setContractError(String(err))}}
  const deleteContract=async(pair:ContractPair)=>{setContractError('');try{const response=await fetch(`/api/v1/contracts/${encodeURIComponent(pair.polymarket_asset_id)}/${encodeURIComponent(pair.kalshi_ticker)}`,{method:'DELETE'});if(!response.ok)throw new Error(`Delete failed (${response.status})`);setContracts(await api<ContractPair[]>('/contracts'));setSpreads(await api<Spread[]>('/spreads'))}catch(err){setContractError(String(err))}}
  useEffect(()=>{
    if(page!=='wallets')return
    api<WalletRanking[]>(`/analytics/wallet-rankings?horizon=${rankingHorizon}&min_trades=${minSampleFilter}`).then(setWalletRankings).catch(()=>setWalletRankings([]))
    api<MarkoutDistribution>(`/analytics/markout-distribution?horizon=${rankingHorizon}`).then(setMarkoutDist).catch(()=>setMarkoutDist(null))
  },[page,rankingHorizon,minSampleFilter,health?.journal.snapshots])
  useEffect(()=>{
    if(page!=='scanner')return
    api<CrossVenueAnalyticsData>('/cross-venue/analytics?window=24h').then(setCrossVenueAnalytics).catch(()=>setCrossVenueAnalytics(null))
  },[page,scannerSubView,health?.journal.snapshots])
  useEffect(()=>{
    if(!wallet){setMarkout(null);setWalletProfile(null);setWalletTrades([]);return}
    api<Markout>(`/wallets/${encodeURIComponent(wallet)}/markouts`).then(setMarkout).catch(()=>setMarkout(null))
    api<WalletProfile>(`/wallets/${encodeURIComponent(wallet)}`).then(setWalletProfile).catch(()=>setWalletProfile(null))
    api<WalletTradeEntry[]>(`/wallets/${encodeURIComponent(wallet)}/trades?limit=50`).then(setWalletTrades).catch(()=>setWalletTrades([]))
  },[wallet,health?.journal.snapshots])
  const activeMarketId=active?.market_id||(books[0]?.market_id??'')
  useEffect(()=>{
    if(page!=='analytics'||!activeMarketId)return
    let alive=true;setAnLoading(true)
    Promise.all([
      api<MarketSummary>(`/analytics/market/${encodeURIComponent(activeMarketId)}/summary?window=${anWin}`).catch(()=>null),
      api<Candle[]>(`/analytics/market/${encodeURIComponent(activeMarketId)}/candles?resolution=${anRes}`).catch(()=>[]),
      api<SpreadPoint[]>(`/analytics/market/${encodeURIComponent(activeMarketId)}/spread?window=${anWin}`).catch(()=>[]),
      api<StorageStats>('/analytics/storage').catch(()=>null)
    ]).then(([s,c,sp,st])=>{
      if(!alive)return
      setAnSummary(s);setAnCandles(c||[]);setAnSpread(sp||[]);setStorageStats(st);setAnLoading(false)
    })
    return()=>{alive=false}
  },[page,activeMarketId,anWin,anRes])
  useEffect(()=>{const key=(e:KeyboardEvent)=>{if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='k'){e.preventDefault();setCommandOpen(v=>!v)}else if(e.key==='Escape')setCommandOpen(false);else if(!e.ctrlKey&&!e.metaKey&&!/input|textarea/i.test((e.target as HTMLElement).tagName)){const item=nav.find(n=>n.shortcut===e.key);if(item)setPage(item.id)}};window.addEventListener('keydown',key);return()=>window.removeEventListener('keydown',key)},[])
  const title=nav.find(n=>n.id===page)?.label||'Overview'
  const subtitle:Record<Page,string>={overview:'A live view across your prediction markets',orderbook:'Price levels, depth and liquidity in real time',scanner:'Compare verified equivalent outcomes across venues',wallets:'Post-trade performance across three horizons',trades:'Explore observed executions and trade records',analytics:'Historical OHLCV, volume VWAP, and storage telemetry',health:'Feed continuity, storage and processing status'}

  return <div className="app">
    <aside className="sidebar">
      <div className="brand"><div className="brand-mark"><Activity size={20} strokeWidth={2.5}/></div><div><strong>POLYPULSE</strong><small>MARKET INTELLIGENCE</small></div></div>
      <div className="workspace-label">WORKSPACE <ChevronDown size={12}/></div>
      <nav>{nav.map(n=><button key={n.id} className={`nav-item ${page===n.id?'active':''}`} onClick={()=>setPage(n.id)}><n.icon size={17}/><span>{n.label}</span><kbd>{n.shortcut}</kbd></button>)}</nav>
      <div className="sidebar-bottom"><div className="sidebar-section">DATA SOURCES</div>{['polymarket','kalshi','polygon'].filter(v=>v!=='polygon'||mode==='live').map(v=><div className="source-row" key={v} title={`Last event: ${lastEvent(health?.feed_details?.[v]?.last_received_timestamp)}`}><span className={`status-dot ${health?.feeds[v]==='connected'||health?.feeds[v]==='demo'?'good':''}`}/><span>{venueName(v)}</span><em>{health?.feeds[v]||'waiting'}</em></div>)}<div className="sidebar-help"><CircleHelp size={16}/> PolyPulse v0.2 <span>⌘ K</span></div></div>
    </aside>
    <main className="main">
      <header className="topbar"><div className="crumb">WORKSPACE <span>/</span> <b>{title.toUpperCase()}</b></div><div className="top-actions"><button className="search-trigger" onClick={()=>setCommandOpen(true)}><Search size={15}/> Search markets or pages <kbd>⌘ K</kbd></button><span className="top-time">{clock.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',hour12:false})}</span><span className={`connection ${socket==='connected'?'online':''}`}><Radio size={14}/>{socket==='connected'?'API CONNECTED':'API RECONNECTING'}</span></div></header>
      <div className="content"><div className="page-heading"><div><div className="eyebrow">POLYPULSE TERMINAL <span>/</span> {page.toUpperCase()}</div><h1>{title}</h1><p>{subtitle[page]}</p></div><div className="heading-right"><span className="date-label">{clock.toLocaleDateString([], {weekday:'short',month:'short',day:'numeric',year:'numeric'})}</span><span className="mode-pill"><span className={`status-dot ${mode==='demo'?'good':''}`}/>{mode==='demo'?'DEMO DATA':mode==='live'?'LIVE MODE':'MODE UNKNOWN'}</span></div></div>
      {error&&<div className="alert">API unavailable: {error}. Start the backend on port 8000.<button onClick={()=>setError('')}><X size={15}/></button></div>}
      {mode==='demo'&&<div className="demo-banner"><ShieldCheck size={16}/><span>DEMO ENVIRONMENT</span> All prices and trades shown here are synthetic sample data.</div>}
      {mode==='live'&&<div className="demo-banner"><Radio size={16}/><span>LIVE MODE</span> Books awaiting a snapshot, recovering, or stale are hidden from quotes and comparisons.</div>}
      {page==='overview'&&<>
        <div className="stats-grid"><Stat label="TRACKED MARKETS" value={String(totalMarkets).padStart(2,'0')} note="Across exchange feeds" icon={<BookOpen size={18}/>} /><Stat label="AVG. BOOK SPREAD" value={signed(avgSpread)} note="Across valid books" icon={<ArrowUpRight size={18}/>} /><Stat label="EVENTS PROCESSED" value={(health?.counters.events||0).toLocaleString()} note="Since service start" icon={<Activity size={18}/>} /><Stat label="FEED STATUS" value={Object.entries(health?.feeds||{}).filter(([v,x])=>v!=='polygon'&&(x==='connected'||x==='demo')).length+'/2'} note="Exchange feeds connected" icon={<Radio size={18}/>} /></div>
        <div className="two-col"><section className="panel"><PanelHead eyebrow="MARKET WATCH" title="Live markets" action={<button className="text-btn" onClick={()=>setPage('orderbook')}>View orderbook →</button>}/><div className="table-wrap"><table><thead><tr><th>MARKET</th><th>VENUE</th><th>STATUS</th><th className="num">BID</th><th className="num">ASK</th><th className="num">SPREAD</th></tr></thead><tbody>{books.map(b=><tr key={`${b.venue}:${b.market_id}`} onClick={()=>{setSelected(`${b.venue}:${b.market_id}`);setPage('orderbook')}}><td className="market-name">{short(b.market_id)}</td><td><Venue name={b.venue}/></td><td>{bookReliable(b)?'VALID':bookStale(b)?'STALE':(b.recovery_status||'RECOVERING').toUpperCase()}</td><td className="num green">{price(bookReliable(b)?b.best_bid:null)}</td><td className="num red">{price(bookReliable(b)?b.best_ask:null)}</td><td className="num">{!bookReliable(b)||b.best_bid==null||b.best_ask==null?'—':signed(b.best_ask-b.best_bid)}</td></tr>)}</tbody></table>{!books.length&&<Empty text="Waiting for the first orderbook snapshot"/>}</div></section>
        <section className="panel"><PanelHead eyebrow="CROSS-VENUE" title="Price opportunities" action={<button className="text-btn" onClick={()=>setPage('scanner')}>Open scanner →</button>}/><div className="opportunity-list">{sortedSpreads.map((s,i)=>{const edge=Math.max(s.buy_polymarket_sell_kalshi??-2,s.buy_kalshi_sell_polymarket??-2);return <div className="opportunity" key={s.name}><div className="op-index">0{i+1}</div><div><strong>{s.name}</strong><small>{edge>0?'Gross quote difference':'No positive gross quote difference'}</small></div><div className={`op-edge ${edge>0?'green':''}`}>{signed(edge)}</div></div>})}{!spreads.length&&<Empty text="Configure verified market pairs to compare venues"/>}</div><div className="panel-foot">Quote differences exclude fees and execution constraints.</div></section></div>
        <div className="bottom-grid"><section className="panel compact"><div className="mini-icon blue"><Database size={19}/></div><div><div className="mini-label">PERSISTED EVENTS</div><strong>{(health?.journal.events||0).toLocaleString()}</strong><span> in local journal</span></div></section><section className="panel compact"><div className="mini-icon purple"><Gauge size={19}/></div><div><div className="mini-label">SNAPSHOT RECOVERIES</div><strong>{health?.counters.recoveries||0}</strong><span> total</span></div></section><section className="panel compact"><div className="mini-icon teal"><Wallet size={19}/></div><div><div className="mini-label">TRACKED WALLETS</div><strong>{wallets.length}</strong><span> with trade records</span></div></section></div>
      </>}
      {page==='orderbook'&&<><div className="toolbar"><label className="input-search"><Search size={16}/><input placeholder="Filter markets..." value={search} onChange={e=>setSearch(e.target.value)}/></label><select value={active?`${active.venue}:${active.market_id}`:''} onChange={e=>setSelected(e.target.value)}>{filtered.map(b=><option key={`${b.venue}:${b.market_id}`} value={`${b.venue}:${b.market_id}`}>{venueName(b.venue)} · {short(b.market_id)}</option>)}</select></div>{active?<><div className="panel-foot">Book: {bookReliable(active)?'VALID':bookStale(active)?'STALE':(active.recovery_status||'RECOVERING').toUpperCase()} · Last event: {lastEvent(active.last_received_timestamp)} · {venueName(active.venue)} feed: {health?.feeds[active.venue]||'waiting'}</div><div className="stats-grid three"><Stat label="BEST BID" value={price(bookReliable(active)?active.best_bid:null)} note={venueName(active.venue)} icon={<ArrowUpRight size={18}/>} tone="green"/><Stat label="BEST ASK" value={price(bookReliable(active)?active.best_ask:null)} note="Normalized YES contract" icon={<ArrowDownRight size={18}/>} tone="red"/><Stat label="SPREAD" value={!bookReliable(active)||active.best_bid==null||active.best_ask==null?'—':signed(active.best_ask-active.best_bid)} note={`Sequence ${active.sequence}`} icon={<Activity size={18}/>} /></div><div className="two-col"><Ladder title="Ask depth" side="ask" levels={bookReliable(active)?active.asks:[]}/><Ladder title="Bid depth" side="bid" levels={bookReliable(active)?active.bids:[]}/></div><div className="bottom-grid two"><section className="panel compact"><div className="mini-icon blue"><BarChart3 size={19}/></div><div><div className="mini-label">BUY VWAP · 10 CONTRACTS</div><strong>{price(bookReliable(active)?active.buy_vwap_10:null)}</strong></div></section><section className="panel compact"><div className="mini-icon purple"><Activity size={19}/></div><div><div className="mini-label">ORDER FLOW IMBALANCE</div><strong>{bookReliable(active)?(active.imbalance*100).toFixed(1)+'%':'—'}</strong></div></section></div></>:<Empty text="No orderbook is available yet"/>}</>}
      {page==='scanner'&&(()=>{
        const activeSpread = spreads.find(s => (s.pair_id || `${s.polymarket.market_id}:${s.kalshi.market_id}` || s.name) === selectedSpreadPair) || sortedSpreads[0]
        const filteredSpreads = sortedSpreads.filter(s => {
          if (spreadStatusFilter === 'all') return true
          if (spreadStatusFilter === 'executable') return s.status === 'executable' || s.is_arbitrage
          if (spreadStatusFilter === 'candidate') return s.status === 'candidate'
          if (spreadStatusFilter === 'stale') return s.status === 'stale'
          if (spreadStatusFilter === 'insufficient_liquidity') return s.status === 'insufficient_liquidity'
          return true
        })
        const execCount = sortedSpreads.filter(s => s.status === 'executable' || s.is_arbitrage).length
        const maxNetSpread = sortedSpreads.length ? Math.max(...sortedSpreads.map(s => s.net_spread ?? s.best_spread ?? -1)) : null
        const maxNetBps = sortedSpreads.length ? Math.max(...sortedSpreads.map(s => s.best_spread_bps ?? 0)) : null

        return <>
          <div className="demo-banner">
            <ShieldCheck size={16}/>
            <span>CROSS-VENUE ARBITRAGE &amp; MICROSTRUCTURE SCANNER</span>
            Enforces strict contract equivalence, depth-aware execution VWAP, non-linear Kalshi taker fee formulas, quote freshness synchronization, and 8-state opportunity classification.
          </div>

          <div className="stats-grid">
            <Stat label="REVIEWED PAIRS" value={String(contracts.length).padStart(2,'0')} note="Operator verified" icon={<BookOpen size={18}/>} />
            <Stat label="EXECUTABLE ARBITRAGE" value={String(execCount).padStart(2,'0')} note="Net positive after fees" icon={<Activity size={18}/>} tone={execCount > 0 ? "green" : undefined} />
            <Stat label="MAX NET SPREAD" value={maxNetBps != null && maxNetBps > 0 ? `${maxNetBps.toFixed(0)} bps` : '—'} note={maxNetSpread != null ? signed(maxNetSpread) : 'Across pairs'} icon={<ArrowUpRight size={18}/>} tone={maxNetBps != null && maxNetBps > 0 ? "green" : undefined} />
            <Stat label="SCANNER SYNC" value={health?.feeds['kalshi']==='connected'||health?.feeds['kalshi']==='demo' ? 'SYNCED' : 'PARTIAL'} note={`Δ max 5s threshold`} icon={<Gauge size={18}/>} />
          </div>

          <div className="toolbar" style={{alignItems:'center'}}>
            <div className="time-pills">
              <button className={`time-pill ${scannerSubView==='scanner'?'active':''}`} onClick={()=>setScannerSubView('scanner')}>OPPORTUNITY SCANNER</button>
              <button className={`time-pill ${scannerSubView==='analytics'?'active':''}`} onClick={()=>setScannerSubView('analytics')}>HISTORICAL ANALYTICS</button>
              <button className={`time-pill ${scannerSubView==='registry'?'active':''}`} onClick={()=>setScannerSubView('registry')}>OPERATOR REGISTRY</button>
            </div>
            {scannerSubView==='scanner' && <div className="time-pills">
              <button className={`time-pill ${spreadStatusFilter==='all'?'active':''}`} onClick={()=>setSpreadStatusFilter('all')}>ALL</button>
              <button className={`time-pill ${spreadStatusFilter==='executable'?'active':''}`} onClick={()=>setSpreadStatusFilter('executable')}>EXECUTABLE</button>
              <button className={`time-pill ${spreadStatusFilter==='candidate'?'active':''}`} onClick={()=>setSpreadStatusFilter('candidate')}>CANDIDATE</button>
              <button className={`time-pill ${spreadStatusFilter==='insufficient_liquidity'?'active':''}`} onClick={()=>setSpreadStatusFilter('insufficient_liquidity')}>LOW LIQ</button>
              <button className={`time-pill ${spreadStatusFilter==='stale'?'active':''}`} onClick={()=>setSpreadStatusFilter('stale')}>STALE</button>
            </div>}
          </div>

          {scannerSubView==='scanner' && <>
            <section className="panel" style={{marginBottom:'19px'}}>
              <PanelHead eyebrow="OPPORTUNITY SCANNER (CLICK PAIR TO INSPECT)" title="Cross-Venue Spread &amp; Execution Table"/>
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>EVENT / OUTCOME</th>
                      <th>MAP</th>
                      <th>ROUTE</th>
                      <th className="num">POLY BID/ASK</th>
                      <th className="num">KALSHI BID/ASK</th>
                      <th className="num">GROSS SPREAD</th>
                      <th className="num">NET SPREAD</th>
                      <th className="num">DEPTH</th>
                      <th>QUOTE AGE &amp; Δ</th>
                      <th>STATUS</th>
                    </tr>
                  </thead>
                  <tbody>
                    {filteredSpreads.map(s => {
                      const pairKey = s.pair_id || `${s.polymarket.market_id}:${s.kalshi.market_id}` || s.name
                      const isChosen = (activeSpread?.pair_id || `${activeSpread?.polymarket.market_id}:${activeSpread?.kalshi.market_id}` || activeSpread?.name) === pairKey
                      const isExec = s.status === 'executable' || s.is_arbitrage
                      const statusClass = isExec ? 'badge-green' : s.status === 'stale' ? 'badge-red' : s.status === 'insufficient_liquidity' ? 'badge-yellow' : 'badge-cyan'
                      const opp = s.opportunity_type || 'none'
                      const route = opp.includes('poly') && !opp.includes('short_poly') ? 'Poly → Kalshi' : opp.includes('kalshi') && !opp.includes('short_kalshi') ? 'Kalshi → Poly' : opp === 'mint_both_long' ? 'Mint Both' : opp === 'merge_both_short' ? 'Merge Both' : 'Neutral'
                      return (
                        <tr key={pairKey} className={isChosen ? 'chosen' : ''} onClick={()=>setSelectedSpreadPair(pairKey)}>
                          <td className="market-name">
                            <strong>{s.canonical_event || s.name}</strong><br/>
                            <small style={{color:'var(--text-muted)'}}>{s.canonical_outcome || (s.equivalence_mapping==='inverse'?'Poly YES ↔ Kalshi NO':'Direct YES ↔ YES')}</small>
                          </td>
                          <td><span className={`badge ${s.equivalence_mapping==='inverse'?'badge-purple':'badge-cyan'}`}>{(s.equivalence_mapping||'direct').toUpperCase()}</span></td>
                          <td><span className="mono" style={{fontSize:'10px',color:'#c3d0e0'}}>{route}</span></td>
                          <td className="num">{price(s.polymarket.best_bid)} / {price(s.polymarket.best_ask)}</td>
                          <td className="num">{price(s.kalshi.best_bid)} / {price(s.kalshi.best_ask)}</td>
                          <td className={`num ${(s.best_spread??0)>0?'green':'red'}`}>
                            <strong>{signed(s.best_spread)}</strong>
                            {s.best_spread_bps!=null&&<><br/><small>{s.best_spread_bps>0?'+':''}{s.best_spread_bps.toFixed(0)} bps</small></>}
                          </td>
                          <td className={`num ${(s.net_spread??s.best_spread??0)>0?'green':'red'}`}>
                            <strong>{signed(s.net_spread ?? s.best_spread)}</strong>
                          </td>
                          <td className="num">{s.executable_qty!=null?s.executable_qty.toLocaleString():'—'}</td>
                          <td>
                            <span className="mono" style={{fontSize:'10px',color: (s.quote_age_seconds?.venue_delta ?? 0) > 5 ? '#efaaaa' : '#94a3b8'}}>
                              {s.quote_age_seconds ? `P:${s.quote_age_seconds.polymarket}s K:${s.quote_age_seconds.kalshi}s Δ:${s.quote_age_seconds.venue_delta}s` : '—'}
                            </span>
                          </td>
                          <td><span className={`badge ${statusClass}`}>{(s.status || (isExec ? 'EXECUTABLE' : 'CANDIDATE')).replace(/_/g,' ').toUpperCase()}</span></td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
                {!spreads.length && <Empty text="No verified contract pairs currently streaming valid orderbooks"/>}
              </div>
              <div className="panel-foot">Click any pair above to inspect orderbook ladders, multi-size depth VWAP, fee haircuts, and settlement criteria.</div>
            </section>

            {activeSpread && <div className="two-col">
              <section className="panel">
                <PanelHead eyebrow="MARKET ORDERBOOK COMPARISON" title={`${activeSpread.canonical_event || activeSpread.name}`}/>
                <div style={{padding:'14px 20px',borderBottom:'1px solid #263448',fontSize:'11px',color:'#8c9eb0',display:'flex',gap:'15px',flexWrap:'wrap'}}>
                  <div>MAPPING: <span className={`badge ${activeSpread.equivalence_mapping==='inverse'?'badge-purple':'badge-cyan'}`}>{(activeSpread.equivalence_mapping||'direct').toUpperCase()}</span></div>
                  <div>POLY BID/ASK: <strong style={{color:'#e8edf4'}}>{price(activeSpread.polymarket.best_bid)} / {price(activeSpread.polymarket.best_ask)}</strong></div>
                  <div>KALSHI BID/ASK: <strong style={{color:'#e8edf4'}}>{price(activeSpread.kalshi.best_bid)} / {price(activeSpread.kalshi.best_ask)}</strong></div>
                </div>
                <div style={{display:'grid',gridTemplateColumns:'1fr 1fr',gap:'1px',background:'#263448'}}>
                  <div style={{background:'#121b27',padding:'12px'}}>
                    <div style={{color:'#8bb8f4',font:'600 11px IBM Plex Mono',marginBottom:'8px'}}>POLYMARKET BOOK</div>
                    <Ladder title="Poly Asks" side="ask" levels={activeSpread.polymarket.asks || []}/>
                    <Ladder title="Poly Bids" side="bid" levels={activeSpread.polymarket.bids || []}/>
                  </div>
                  <div style={{background:'#121b27',padding:'12px'}}>
                    <div style={{color:'#a7e1ae',font:'600 11px IBM Plex Mono',marginBottom:'8px'}}>KALSHI BOOK</div>
                    <Ladder title="Kalshi Asks" side="ask" levels={activeSpread.kalshi.asks || []}/>
                    <Ladder title="Kalshi Bids" side="bid" levels={activeSpread.kalshi.bids || []}/>
                  </div>
                </div>
              </section>

              <div>
                <section className="panel" style={{marginBottom:'19px'}}>
                  <PanelHead eyebrow="DEPTH-AWARE VWAP &amp; FEE BREAKDOWN" title="Execution Ladder"/>
                  {activeSpread.vwap_by_size ? <div className="table-wrap">
                    <table>
                      <thead>
                        <tr>
                          <th>SIZE</th>
                          <th>STATUS</th>
                          <th className="num">BUY VWAP</th>
                          <th className="num">SELL VWAP</th>
                          <th className="num">VWAP SPREAD</th>
                        </tr>
                      </thead>
                      <tbody>
                        {Object.entries(activeSpread.vwap_by_size).map(([sz, res]) => (
                          <tr key={sz}>
                            <td className="mono">{sz} UNITS</td>
                            <td><span className={`badge ${res.fillable ? 'badge-green' : 'badge-yellow'}`}>{res.fillable ? 'FILLABLE' : `MAX: ${res.max_available ?? 0}`}</span></td>
                            <td className="num">{price(res.buy_vwap ?? res.poly_ask_vwap ?? res.kalshi_ask_vwap)}</td>
                            <td className="num">{price(res.sell_vwap ?? res.kalshi_bid_vwap ?? res.poly_bid_vwap)}</td>
                            <td className={`num ${(res.vwap_spread ?? 0) > 0 ? 'green' : 'red'}`}>{signed(res.vwap_spread)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div> : <div style={{padding:'16px',color:'#718398'}}>Ladder VWAP available for verified active depth.</div>}
                </section>

                <section className="panel">
                  <PanelHead eyebrow="SETTLEMENT DETAILS &amp; DIAGNOSTICS" title="Contract Evidence"/>
                  <div style={{padding:'16px 20px',fontSize:'11px',display:'grid',gap:'9px'}}>
                    <div><strong>RESOLUTION SOURCE:</strong> <span style={{color:'#c3d0e0'}}>{activeSpread.resolution_source || 'Authoritative source required'}</span></div>
                    <div><strong>CRITERIA:</strong> <span style={{color:'#c3d0e0'}}>{activeSpread.resolution_criteria || 'Matching on title alone is strictly prohibited.'}</span></div>
                    <div><strong>EXPIRATION:</strong> <span className="mono" style={{color:'#c3d0e0'}}>{activeSpread.expiration ? new Date(activeSpread.expiration).toLocaleString() : '—'}</span></div>
                    <div><strong>FEE SCHEDULE:</strong> <span className="mono" style={{color:'#c3d0e0'}}>v2026.1 (Kalshi CFTC taker formula + Polymarket 0bps CLOB + 10bps slippage)</span></div>
                    {activeSpread.status !== 'executable' && <div className="alert" style={{margin:'8px 0 0'}}>
                      <span>REJECTION REASON:</span> {activeSpread.status === 'candidate' ? 'Gross spread is positive but net spread after known trading fees and slippage is ≤ 0.' : activeSpread.status === 'stale' ? 'Quotes exceed maximum freshness threshold (10s quote age or 5s cross-venue delta).' : activeSpread.status === 'insufficient_liquidity' ? 'Top-of-book depth is 0 at one or both venues.' : 'Contract verification or book integrity pending.'}
                    </div>}
                  </div>
                </section>
              </div>
            </div>}
          </>}

          {scannerSubView==='analytics' && <>
            <div className="stats-grid">
              <Stat label="HISTORICAL OBS" value={crossVenueAnalytics?.quote_freshness?.sample_count ? String(crossVenueAnalytics.quote_freshness.sample_count).padStart(2,'0') : '0'} note="24h telemetry window" icon={<Database size={18}/>} />
              <Stat label="MEDIAN OPPORTUNITY" value={crossVenueAnalytics?.duration_stats?.median_duration_seconds != null ? `${crossVenueAnalytics.duration_stats.median_duration_seconds.toFixed(1)}s` : '—'} note="Duration available" icon={<Activity size={18}/>} />
              <Stat label="POLY AVERAGE QUOTE AGE" value={crossVenueAnalytics?.quote_freshness?.avg_quote_age_poly != null ? `${crossVenueAnalytics.quote_freshness.avg_quote_age_poly.toFixed(2)}s` : '—'} note="Freshness latency" icon={<Gauge size={18}/>} />
              <Stat label="VENUE DESYNC EVENTS" value={String(crossVenueAnalytics?.quote_freshness?.desync_count ?? 0)} note="Δ > 5.0s desynchronization" icon={<ArrowDownRight size={18}/>} tone={(crossVenueAnalytics?.quote_freshness?.desync_count ?? 0) > 0 ? "red" : "green"} />
            </div>

            <section className="panel" style={{marginBottom:'19px'}}>
              <PanelHead eyebrow="TOP VERIFIED SPREAD PAIRS (CLICKHOUSE HISTORICAL)" title="Top Historical Spreads (N ≥ 5 Guardrail)"/>
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>PAIR ID</th>
                      <th>CANONICAL EVENT</th>
                      <th>MAP</th>
                      <th className="num">OBS</th>
                      <th className="num">AVG GROSS</th>
                      <th className="num">AVG NET SPREAD</th>
                      <th className="num">MAX NET</th>
                      <th className="num">AVG DEPTH</th>
                      <th className="num">EXEC RATE %</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(crossVenueAnalytics?.top_spread_pairs || []).map(p => (
                      <tr key={p.pair_id}>
                        <td className="mono"><strong>{p.pair_id}</strong></td>
                        <td className="market-name">{p.canonical_event}</td>
                        <td><span className={`badge ${p.equivalence_mapping==='inverse'?'badge-purple':'badge-cyan'}`}>{p.equivalence_mapping.toUpperCase()}</span></td>
                        <td className="num">{p.total_observations}</td>
                        <td className="num">{signed(p.avg_gross_spread)}</td>
                        <td className={`num ${p.avg_net_spread > 0 ? 'green' : 'red'}`}><strong>{signed(p.avg_net_spread)}</strong></td>
                        <td className="num green">{signed(p.max_net_spread)}</td>
                        <td className="num">{p.avg_executable_qty.toLocaleString()}</td>
                        <td className="num">{p.executable_rate_pct.toFixed(1)}%</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                {!(crossVenueAnalytics?.top_spread_pairs || []).length && <Empty text="Insufficient historical observations recorded in ClickHouse yet"/>}
              </div>
            </section>

            <div className="two-col">
              <section className="panel">
                <PanelHead eyebrow="OPPORTUNITY FREQUENCY &amp; FEE EROSION" title="Fee Impact &amp; Staleness Breakdown"/>
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th>PAIR ID</th>
                        <th className="num">GROSS ARBS</th>
                        <th className="num">FEE ERODED</th>
                        <th className="num">FEE EROSION %</th>
                        <th className="num">STALE REJECTS</th>
                      </tr>
                    </thead>
                    <tbody>
                      {(crossVenueAnalytics?.opportunity_frequency || []).map(f => (
                        <tr key={f.pair_id}>
                          <td className="mono"><strong>{f.pair_id}</strong></td>
                          <td className="num">{f.gross_positive_count}</td>
                          <td className="num red">{f.fee_eroded_count}</td>
                          <td className="num red"><strong>{f.fee_erosion_pct.toFixed(1)}%</strong></td>
                          <td className="num">{f.stale_rejected_count}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  {!(crossVenueAnalytics?.opportunity_frequency || []).length && <Empty text="No opportunity frequency records available"/>}
                </div>
              </section>

              <section className="panel">
                <PanelHead eyebrow="VENUE PRICING EDGE" title="Route Advantage"/>
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th>BUY VENUE</th>
                        <th>SELL VENUE</th>
                        <th className="num">OPPORTUNITIES</th>
                        <th className="num">AVG GROSS</th>
                        <th className="num">AVG NET</th>
                        <th className="num">AVG DEPTH</th>
                      </tr>
                    </thead>
                    <tbody>
                      {(crossVenueAnalytics?.venue_comparison || []).map((v, i) => (
                        <tr key={i}>
                          <td><Venue name={v.buy_venue}/></td>
                          <td><Venue name={v.sell_venue}/></td>
                          <td className="num">{v.opportunity_count}</td>
                          <td className="num">{signed(v.avg_gross_spread)}</td>
                          <td className={`num ${v.avg_net_spread > 0 ? 'green' : 'red'}`}>{signed(v.avg_net_spread)}</td>
                          <td className="num">{v.avg_available_qty.toLocaleString()}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  {!(crossVenueAnalytics?.venue_comparison || []).length && <Empty text="No venue comparison records available"/>}
                </div>
              </section>
            </div>
          </>}

          {scannerSubView==='registry' && <>
            <section className="panel">
              <PanelHead eyebrow="OPERATOR REVIEW" title="Verified Contract Registry"/>
              <form className="contract-form" onSubmit={saveContract}>
                <label>Polymarket asset ID<input required value={contractForm.polymarket_asset_id} onChange={e=>setContractForm({...contractForm,polymarket_asset_id:e.target.value})}/></label>
                <label>Kalshi ticker<input required value={contractForm.kalshi_ticker} onChange={e=>setContractForm({...contractForm,kalshi_ticker:e.target.value})}/></label>
                <label>Canonical Event<input required placeholder="e.g. FOMC Target Rate Decision" value={contractForm.canonical_event||contractForm.event_description} onChange={e=>setContractForm({...contractForm,canonical_event:e.target.value,event_description:e.target.value})}/></label>
                <label>Canonical Outcome<input placeholder="e.g. Rate cuts ≥ 25 bps" value={contractForm.canonical_outcome||contractForm.outcome_equivalence} onChange={e=>setContractForm({...contractForm,canonical_outcome:e.target.value,outcome_equivalence:e.target.value})}/></label>
                <label>Equivalence Mapping<select value={contractForm.equivalence_mapping||'direct'} onChange={e=>setContractForm({...contractForm,equivalence_mapping:e.target.value as 'direct'|'inverse'})}><option value="direct">Direct (Poly YES == Kalshi YES)</option><option value="inverse">Inverse (Poly YES == Kalshi NO)</option></select></label>
                <label>Resolution source<input required placeholder="e.g. Federal Reserve H.4.1 Release" value={contractForm.resolution_source} onChange={e=>setContractForm({...contractForm,resolution_source:e.target.value})}/></label>
                <label>Resolution criteria<textarea placeholder="Explicit textual criteria required for verification" value={contractForm.resolution_criteria||''} onChange={e=>setContractForm({...contractForm,resolution_criteria:e.target.value})}/></label>
                <label>Settlement details<input placeholder="e.g. Polymarket USDC via UMA; Kalshi USD under CFTC" value={contractForm.settlement_details||''} onChange={e=>setContractForm({...contractForm,settlement_details:e.target.value})}/></label>
                <label>Expiration (UTC)<input type="datetime-local" required value={contractForm.expiration.slice(0,16)} onChange={e=>setContractForm({...contractForm,expiration:e.target.value})}/></label>
                <label>Reviewer ID<input placeholder="Operator identity (required if verified)" value={contractForm.reviewer||''} onChange={e=>setContractForm({...contractForm,reviewer:e.target.value})}/></label>
                <label>Verification status<select value={contractForm.verification_status} onChange={e=>setContractForm({...contractForm,verification_status:e.target.value as ContractPair['verification_status']})}><option value="unverified">Unverified</option><option value="verified">Verified by operator</option><option value="rejected">Rejected</option><option value="incompatible">Incompatible</option></select></label>
                <div className="contract-actions"><button type="submit">Save review</button><button type="button" onClick={()=>setContractForm(emptyContract)}>Clear</button></div>
              </form>
              {contractError&&<div className="alert">{contractError}</div>}
              <div className="contract-list">
                {contracts.map(pair=>(
                  <div key={`${pair.polymarket_asset_id}:${pair.kalshi_ticker}`}>
                    <strong>{pair.canonical_event||pair.event_description}</strong>
                    <span><span className={`badge ${pair.verification_status==='verified'?'badge-green':pair.verification_status==='rejected'?'badge-red':'badge-muted'}`}>{pair.verification_status.toUpperCase()}</span> · <span className="badge badge-purple">{(pair.equivalence_mapping||'direct').toUpperCase()}</span> · {short(pair.polymarket_asset_id)} ↔ {pair.kalshi_ticker}</span>
                    <span>Outcome: {pair.canonical_outcome||pair.outcome_equivalence}</span>
                    <span>Source: {pair.resolution_source} · Expires {new Date(pair.expiration).toLocaleString()}</span>
                    {pair.reviewer&&<span>Reviewed by: {pair.reviewer} {pair.verified_at?`on ${new Date(pair.verified_at).toLocaleDateString()}`:''}</span>}
                    <button onClick={()=>setContractForm({...pair,expiration:pair.expiration.slice(0,16)})}>Edit</button>
                    <button onClick={()=>deleteContract(pair)}>Delete</button>
                  </div>
                ))}
              </div>
              {!contracts.length&&<Empty text="No manually reviewed contract pairs"/>}
            </section>
          </>}
        </>
      })()}
      {page==='wallets'&&<>
        <div className="demo-banner">
          <ShieldCheck size={16}/>
          <span>ADVERSE SELECTION ENGINE</span>
          Execution markout evaluates post-trade market drift across 5s, 30s, and 60s observation horizons. Measures adverse selection; does not reflect realized portfolio P&L.
        </div>
        <div className="stats-grid">
          <Stat label="TRACKED WALLETS" value={String(wallets.length).padStart(2,'0')} note="Attributed to fills" icon={<Wallet size={18}/>} />
          <Stat label="ACTIVE HORIZON" value={`${rankingHorizon}S`} note="Adverse selection window" icon={<Gauge size={18}/>} />
          <Stat label="MEDIAN SAMPLE DRIFT" value={signedBps(markoutDist?.quantiles?.median)} note="Across active trades" icon={<ArrowUpRight size={18}/>} tone={markoutDist?.quantiles?.median!=null && markoutDist.quantiles.median >= 0 ? "green" : "red"} />
          <Stat label="MARKOUT WIN RATE" value={markoutDist?.win_rate!=null?`${markoutDist.win_rate.toFixed(1)}%`:'—'} note="Positive drift %" icon={<Activity size={18}/>} />
        </div>
        <div className="toolbar" style={{alignItems:'center'}}>
          <label className="input-search">
            <Search size={16}/>
            <input placeholder="Filter wallets by address..." value={walletSearch} onChange={e=>setWalletSearch(e.target.value)}/>
          </label>
          <div className="time-pills">
            {([5, 30, 60] as const).map(h=><button key={h} className={`time-pill ${rankingHorizon===h?'active':''}`} onClick={()=>setRankingHorizon(h)}>{h}S HORIZON</button>)}
          </div>
          <div className="time-pills">
            <button className={`time-pill ${minSampleFilter===5?'active':''}`} onClick={()=>setMinSampleFilter(5)}>N ≥ 5 (QUALIFIED)</button>
            <button className={`time-pill ${minSampleFilter===1?'active':''}`} onClick={()=>setMinSampleFilter(1)}>ALL SAMPLES</button>
          </div>
        </div>
        <div className="two-col wallet-layout">
          <section className="panel">
            <PanelHead eyebrow="WALLET DIRECTORY & ALPHA RANKINGS" title="Wallet Leaderboard"/>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>#</th>
                    <th>WALLET</th>
                    <th className="num">TOTAL</th>
                    <th className="num">AVAIL</th>
                    <th className="num">AVG DRIFT</th>
                    <th className="num">WIN %</th>
                    <th>STATUS</th>
                  </tr>
                </thead>
                <tbody>
                  {(walletRankings.length ? walletRankings : wallets.map(w=>({
                    wallet: w.wallet,
                    total_trades: w.trade_count,
                    trade_count: w.trade_count,
                    available_count: 0,
                    horizon_seconds: rankingHorizon,
                    avg_markout: null,
                    avg_markout_bps: null,
                    positive_percentage: null,
                    qualified: false,
                    sample_status: 'insufficient_sample'
                  })))
                  .filter(r => !walletSearch || r.wallet.toLowerCase().includes(walletSearch.toLowerCase()))
                  .map((r, idx) => {
                    const isChosen = wallet.toLowerCase() === r.wallet.toLowerCase()
                    const isPos = (r.avg_markout_bps ?? 0) > 0
                    const isNeg = (r.avg_markout_bps ?? 0) < 0
                    return (
                    <tr key={r.wallet} className={isChosen ? 'chosen' : ''} onClick={()=>setWallet(r.wallet)}>
                        <td className="mono">{String(idx + 1).padStart(2, '0')}</td>
                        <td className="mono" title={r.wallet}><strong>{short(r.wallet)}</strong></td>
                        <td className="num">{r.total_trades ?? r.trade_count}</td>
                        <td className="num">{r.available_count ?? 0}</td>
                        <td className={`num ${isPos ? 'green' : isNeg ? 'red' : ''}`}>{signedBps(r.avg_markout_bps)}</td>
                        <td className="num">{r.positive_percentage != null ? `${r.positive_percentage.toFixed(0)}%` : '—'}</td>
                        <td>
                          <span className={`status-pill ${r.qualified ? 'good' : 'muted'}`} title={r.qualified ? `Qualified: ${r.available_count ?? 0} available markouts` : `Insufficient sample: ${r.available_count ?? 0} < 5 available markouts`}>
                            {r.qualified ? 'QUALIFIED' : 'N < 5'}
                          </span>
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
              {!wallets.length && <Empty text="No wallet-attributed trades recorded yet"/>}
            </div>
          </section>

          <div>
            <section className="panel" style={{marginBottom:'19px'}}>
              <PanelHead eyebrow="EXECUTION DRIFT ANALYSIS" title={wallet ? short(wallet) : 'Select a wallet'}/>
              {wallet ? <>
                {walletProfile && <div style={{display:'flex',gap:'15px',flexWrap:'wrap',padding:'14px 20px',borderBottom:'1px solid #263448',fontSize:'11px',color:'#8c9eb0'}}>
                  <div>WALLET: <strong className="mono" style={{color:'#e8edf4'}}>{wallet}</strong></div>
                  <div>VOLUME: <strong style={{color:'#e8edf4'}}>{walletProfile.total_volume.toLocaleString()} units</strong></div>
                  <div>NOTIONAL: <strong style={{color:'#e8edf4'}}>${walletProfile.total_notional.toLocaleString()}</strong></div>
                  <div>TYPE: <span className="badge-attribution maker">{walletProfile.maker_count} Maker</span> <span className="badge-attribution taker">{walletProfile.taker_count} Taker</span></div>
                </div>}

                {markout ? <div className="markout-cards">
                  {markout.summary.map(s => {
                    const isPos = (s.average_markout_bps ?? s.average_markout ?? 0) > 0
                    const isNeg = (s.average_markout_bps ?? s.average_markout ?? 0) < 0
                    return (
                      <div className="markout-card" key={s.horizon_seconds}>
                        <span>{s.horizon_seconds}S POST-TRADE MARKOUT</span>
                        <strong className={isPos ? 'green' : isNeg ? 'red' : ''}>
                          {s.average_markout_bps != null ? signedBps(s.average_markout_bps) : signed(s.average_markout)}
                        </strong>
                        <small>{s.average_markout != null ? signedDollars(s.average_markout) + ' · ' : ''}{s.available_count}/{s.trade_count} obs · {s.positive_percentage == null ? '—' : s.positive_percentage.toFixed(0) + '%'} positive</small>
                        {s.status_counts && <div style={{display:'flex',gap:'4px',marginTop:'8px'}}>
                          <span className="status-pill good" title="Available">{s.status_counts.available ?? 0}</span>
                          <span className="status-pill warn" title="Crossed">{s.status_counts.crossed ?? 0}</span>
                          <span className="status-pill bad" title="Stale">{s.status_counts.stale ?? 0}</span>
                          <span className="status-pill muted" title="Unavailable">{s.status_counts.unavailable ?? 0}</span>
                        </div>}
                      </div>
                    )
                  })}
                </div> : <Empty text="Loading markout observations..."/>}

                {walletTrades.length > 0 && <div className="table-wrap" style={{borderTop:'1px solid #263448'}}>
                  <table>
                    <thead>
                      <tr>
                        <th>TIME</th>
                        <th>MARKET</th>
                        <th>ATTRIBUTION</th>
                        <th>SIDE</th>
                        <th className="num">PRICE</th>
                        <th className="num">NOTIONAL</th>
                      </tr>
                    </thead>
                    <tbody>
                      {walletTrades.slice(0, 8).map(t => (
                        <tr key={t.trade_id}>
                          <td className="mono">{t.received_timestamp ? t.received_timestamp.slice(11, 19) : '—'}</td>
                          <td className="market-name">{short(t.market_id)}</td>
                          <td><span className={`badge-attribution ${t.attribution_type}`}>{t.attribution_type.toUpperCase()}</span></td>
                          <td><span className={`side-tag ${t.side}`}>{t.side.toUpperCase()}</span></td>
                          <td className="num">{price(parseFloat(t.price))}</td>
                          <td className="num">${parseFloat(t.notional).toFixed(2)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>}
                <div className="panel-foot">Post-trade midpoints are sampled at t + H. Rejects crossed/locked orderbooks and stale feeds.</div>
              </> : <Empty text="Select a wallet from the leaderboard to inspect execution quality and post-trade markout"/>}
            </section>

            <section className="panel">
              <PanelHead eyebrow="MICROSTRUCTURE ADVERSE SELECTION" title={`${rankingHorizon}s Drift Distribution`}/>
              <div className="dist-grid">
                <div className="dist-card"><span>P10 (TAIL LOSS)</span><strong className={(markoutDist?.quantiles?.p10 ?? 0) >= 0 ? 'green' : 'red'}>{signedBps(markoutDist?.quantiles?.p10)}</strong></div>
                <div className="dist-card"><span>P25 (LOWER)</span><strong className={(markoutDist?.quantiles?.p25 ?? 0) >= 0 ? 'green' : 'red'}>{signedBps(markoutDist?.quantiles?.p25)}</strong></div>
                <div className="dist-card"><span>MEDIAN (P50)</span><strong className={(markoutDist?.quantiles?.median ?? 0) >= 0 ? 'green' : 'red'}>{signedBps(markoutDist?.quantiles?.median)}</strong></div>
                <div className="dist-card"><span>P75 (UPPER)</span><strong className={(markoutDist?.quantiles?.p75 ?? 0) >= 0 ? 'green' : 'red'}>{signedBps(markoutDist?.quantiles?.p75)}</strong></div>
                <div className="dist-card"><span>P90 (ALPHA TAIL)</span><strong className={(markoutDist?.quantiles?.p90 ?? 0) >= 0 ? 'green' : 'red'}>{signedBps(markoutDist?.quantiles?.p90)}</strong></div>
              </div>
              <div className="panel-foot">
                Range: {signedBps(markoutDist?.min_bps)} to {signedBps(markoutDist?.max_bps)} · Mean: {signedBps(markoutDist?.avg_bps)} · Sample size: {markoutDist?.available_count ?? 0} trades
              </div>
            </section>
          </div>
        </div>
      </>}
      {page==='trades'&&<section className="panel"><PanelHead eyebrow="EXECUTION LOG" title="Recent trades"/><div className="table-wrap"><table><thead><tr><th>TIME</th><th>WALLET</th><th>VENUE</th><th>MARKET</th><th>SIDE</th><th className="num">PRICE</th><th className="num">QUANTITY</th></tr></thead><tbody>{trades.map(t=><tr key={t.trade_id}><td>{time(t.executed_at)}</td><td className="mono">{t.wallet?short(t.wallet):'—'}</td><td><Venue name={t.venue}/></td><td className="market-name">{short(t.market_id)}</td><td><span className={`side-tag ${t.side}`}>{t.side.toUpperCase()}</span></td><td className="num">{price(t.price)}</td><td className="num">{t.quantity==null?'—':t.quantity.toFixed(2)}</td></tr>)}</tbody></table>{!trades.length&&<Empty text="No trade records available yet"/>}</div></section>}
      {page==='analytics'&&<>
        <div className="analytics-controls">
          <label className="input-search">
            <Search size={16}/>
            <input placeholder="Filter markets..." value={search} onChange={e=>setSearch(e.target.value)}/>
          </label>
          <select value={active?`${active.venue}:${active.market_id}`:''} onChange={e=>setSelected(e.target.value)}>
            {filtered.map(b=><option key={`${b.venue}:${b.market_id}`} value={`${b.venue}:${b.market_id}`}>{venueName(b.venue)} · {short(b.market_id)}</option>)}
          </select>
          <div className="time-pills">
            {(['1h','6h','24h','7d'] as const).map(w=><button key={w} className={`time-pill ${anWin===w?'active':''}`} onClick={()=>setAnWin(w)}>{w.toUpperCase()}</button>)}
          </div>
          <div className="time-pills">
            {(['1m','5m','1h','1d'] as const).map(r=><button key={r} className={`time-pill ${anRes===r?'active':''}`} onClick={()=>setAnRes(r)}>{r.toUpperCase()}</button>)}
          </div>
          {anLoading&&<span className="mono" style={{fontSize:'11px',color:'#8c9eb0'}}>Scanning ClickHouse...</span>}
        </div>
        {storageStats?.status==='disabled'&&!anSummary&&<div className="demo-banner"><Database size={16}/><span>CLICKHOUSE OFFLINE</span> ClickHouse analytics service is disabled or unreachable. Start ClickHouse on port 8123 to query historical OLAP aggregates.</div>}
        <div className="stats-grid">
          <Stat label={`TOTAL VOLUME (${anWin.toUpperCase()})`} value={anSummary?('$' + anSummary.dollar_volume.toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2})):'$0.00'} note={anSummary?`${anSummary.total_volume.toLocaleString()} contracts`:'No activity'} icon={<BarChart3 size={18}/>} />
          <Stat label="VOLUME-WEIGHTED AVG" value={price(anSummary?.vwap)} note="VWAP across window" icon={<Activity size={18}/>} />
          <Stat label="BEST BID / ASK" value={anSummary?.best_bid!=null&&anSummary?.best_ask!=null?`${price(anSummary.best_bid)} / ${price(anSummary.best_ask)}`:'—'} note={anSummary?.spread_bps!=null?`${anSummary.spread_bps.toFixed(0)} bps spread`:'No active quote'} icon={<ArrowUpRight size={18}/>} tone="green"/>
          <Stat label="EXECUTED TRADES" value={(anSummary?.trade_count||0).toLocaleString()} note={anSummary?`Buy: ${anSummary.buy_volume.toFixed(1)} | Sell: ${anSummary.sell_volume.toFixed(1)}`:'0 trades'} icon={<ListFilter size={18}/>} />
        </div>
        <div className="two-col">
          <section className="panel">
            <PanelHead eyebrow="PRE-AGGREGATED MATERIALIZED VIEW" title={`OHLCV Candles (${anRes.toUpperCase()})`}/>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>TIME</th>
                    <th className="num">OPEN</th>
                    <th className="num">HIGH</th>
                    <th className="num">LOW</th>
                    <th className="num">CLOSE</th>
                    <th className="num">VOLUME</th>
                    <th className="num">VWAP</th>
                    <th className="num">TRADES</th>
                  </tr>
                </thead>
                <tbody>
                  {anCandles.map((c,i)=>{
                    const isUp = c.close >= c.open
                    return (
                      <tr key={`${c.time}-${i}`}>
                        <td className="mono">{c.time.slice(11,19)}</td>
                        <td className="num">{price(c.open)}</td>
                        <td className="num">{price(c.high)}</td>
                        <td className="num">{price(c.low)}</td>
                        <td className={`num ${isUp?'green':'red'}`}>{price(c.close)}</td>
                        <td className="num">{c.volume.toFixed(2)}</td>
                        <td className="num">{price(c.avg_price)}</td>
                        <td className="num">{c.trade_count}</td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
              {!anCandles.length&&<Empty text="No trade candles available in selected window"/>}
            </div>
          </section>
          <section className="panel">
            <PanelHead eyebrow="ORDERBOOK SPREAD & DEPTH" title="Spread Trajectory"/>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>TIME</th>
                    <th className="num">AVG SPREAD</th>
                    <th className="num">MIN / MAX</th>
                    <th className="num">BID DEPTH</th>
                    <th className="num">ASK DEPTH</th>
                  </tr>
                </thead>
                <tbody>
                  {anSpread.map((s,i)=>(
                    <tr key={`${s.time}-${i}`}>
                      <td className="mono">{s.time.slice(11,19)}</td>
                      <td className="num green">{signed(s.avg_spread)}</td>
                      <td className="num">{price(s.min_spread)} / {price(s.max_spread)}</td>
                      <td className="num">{s.avg_bid_depth!=null?s.avg_bid_depth.toFixed(1):'—'}</td>
                      <td className="num">{s.avg_ask_depth!=null?s.avg_ask_depth.toFixed(1):'—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {!anSpread.length&&<Empty text="No snapshot trajectory records found"/>}
            </div>
          </section>
        </div>
        <section className="panel">
          <PanelHead eyebrow="STORAGE ENGINE & CODEC RETENTION" title="ClickHouse Part & Compression Telemetry"/>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>TABLE</th>
                  <th className="num">ROW COUNT</th>
                  <th className="num">COMPRESSED</th>
                  <th className="num">UNCOMPRESSED</th>
                  <th className="num">COMPRESSION RATIO</th>
                </tr>
              </thead>
              <tbody>
                {(storageStats?.tables||[]).map(t=>(
                  <tr key={t.table}>
                    <td className="market-name mono">{t.table}</td>
                    <td className="num">{t.rows.toLocaleString()}</td>
                    <td className="num">{(t.compressed_bytes/1024).toFixed(1)} KB</td>
                    <td className="num">{(t.uncompressed_bytes/1024).toFixed(1)} KB</td>
                    <td className="num green">{t.compression_ratio.toFixed(2)}x</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {!storageStats?.tables?.length&&<Empty text="ClickHouse storage telemetry unavailable"/>}
          </div>
          <div className="panel-foot">
            Status: {storageStats?.status?.toUpperCase()||'UNKNOWN'} · Total Active Rows: {(storageStats?.total_rows||0).toLocaleString()} · Aggregate Compression: {storageStats?.compression_ratio?storageStats.compression_ratio.toFixed(2)+'x':'—'}
          </div>
        </section>
      </>}
      {page==='health'&&<><div className="stats-grid three"><Stat label="INGESTED EVENTS" value={(health?.journal.events||0).toLocaleString()} note="Durable local journal" icon={<Database size={18}/>} /><Stat label="SEQUENCE GAPS" value={String(health?.counters.gaps||0)} note="Kalshi feed" icon={<Activity size={18}/>} /><Stat label="PENDING EXPORT" value={String(health?.journal.pending_export||0)} note={health?.storage.clickhouse||'ClickHouse disabled'} icon={<Gauge size={18}/>} /></div><div className="two-col"><section className="panel"><PanelHead eyebrow="CONNECTIONS" title="Feed status"/><div className="health-list">{Object.entries(health?.feeds||{}).map(([name,status])=><div key={name}><span><span className={`status-dot ${status==='connected'||status==='demo'?'good':''}`}/>{venueName(name)} · last event {lastEvent(health?.feed_details?.[name]?.last_received_timestamp)}</span><strong>{status.toUpperCase()}</strong></div>)}</div></section><section className="panel"><PanelHead eyebrow="RELIABILITY" title="Pipeline counters"/><div className="health-list"><div><span>Snapshot recoveries</span><strong>{health?.counters.recoveries||0}</strong></div><div><span>Duplicate events</span><strong>{health?.counters.duplicates||0}</strong></div><div><span>WebSocket reconnects</span><strong>{health?.counters.reconnects||0}</strong></div><div><span>Received / processed per second</span><strong>{health?.rates?.events_received_per_second||0} / {health?.rates?.events_processed_per_second||0}</strong></div><div><span>Ingestion lag</span><strong>{health?.ingestion_lag_seconds==null?'—':health.ingestion_lag_seconds.toFixed(2)+'s'}</strong></div><div><span>ClickHouse insert latency</span><strong>{health?.storage.insert_latency_ms==null?'—':health.storage.insert_latency_ms.toFixed(1)+'ms'}</strong></div><div><span>Orderbook processing p95</span><strong>{health?.rates?.orderbook_processing_latency_ms_p95==null?'—':health.rates.orderbook_processing_latency_ms_p95.toFixed(1)+'ms'}</strong></div><div><span>Stored snapshots</span><strong>{health?.journal.snapshots||0}</strong></div><div><span>Service uptime</span><strong>{Math.floor((health?.uptime_seconds||0)/60)}m {Math.floor((health?.uptime_seconds||0)%60)}s</strong></div></div></section></div></>}
      </div>
      <footer className="footer"><span><span className={`status-dot ${socket==='connected'?'good':''}`}/> {socket==='connected'?'Connected to PolyPulse':'Connecting to API'}</span><span>{totalMarkets} markets monitored <i>•</i> {mode==='demo'?'Synthetic data':mode==='live'?'Live venue feeds':'Waiting for mode'} <i>•</i> Local time {clock.toLocaleTimeString()}</span></footer>
    </main>
    {commandOpen&&<div className="modal-backdrop" onClick={()=>setCommandOpen(false)}><div className="command" onClick={e=>e.stopPropagation()}><div className="command-search"><Search size={19}/><input autoFocus value={search} onChange={e=>setSearch(e.target.value)} placeholder="Search pages and markets..."/><kbd>ESC</kbd></div><div className="command-label">PAGES</div>{nav.filter(n=>n.label.toLowerCase().includes(search.toLowerCase())).map(n=><button key={n.id} onClick={()=>{setPage(n.id);setCommandOpen(false);setSearch('')}}><n.icon size={17}/>{n.label}<kbd>{n.shortcut}</kbd></button>)}<div className="command-label">MARKETS</div>{filtered.slice(0,6).map(b=><button key={`${b.venue}:${b.market_id}`} onClick={()=>{setSelected(`${b.venue}:${b.market_id}`);setPage('orderbook');setCommandOpen(false);setSearch('')}}><BookOpen size={17}/>{venueName(b.venue)} · {short(b.market_id)}</button>)}</div></div>}
  </div>
}

function Stat({label,value,note,icon,tone}: {label:string;value:string;note:string;icon:React.ReactNode;tone?:string}){return <div className="stat"><div className="stat-top"><span>{label}</span><div className={`stat-icon ${tone||''}`}>{icon}</div></div><strong>{value}</strong><small>{note}</small></div>}
function PanelHead({eyebrow,title,action}:{eyebrow:string;title:string;action?:React.ReactNode}){return <div className="panel-head"><div><span>{eyebrow}</span><h2>{title}</h2></div>{action}</div>}
function Venue({name}:{name:string}){return <span className={`venue ${name}`}><span className="venue-mark">{name==='polymarket'?'P':'K'}</span>{venueName(name)}</span>}
function Empty({text}:{text:string}){return <div className="empty"><Command size={22}/><p>{text}</p></div>}
function Ladder({title,side,levels}:{title:string;side:'bid'|'ask';levels:number[][]}){const max=Math.max(...levels.map(l=>l[1]),1);return <section className="panel ladder"><PanelHead eyebrow={side==='bid'?'BUY ORDERS':'SELL ORDERS'} title={title}/><div className="ladder-header"><span>PRICE</span><span>SIZE</span><span>DEPTH</span></div>{levels.map(([p,q],i)=><div className="ladder-row" key={`${p}-${i}`}><div className={`depth-bar ${side}`} style={{width:`${q/max*85}%`}}/><strong className={side==='bid'?'green':'red'}>{p.toFixed(4)}</strong><span>{q.toFixed(2)}</span><span>{(q/max*100).toFixed(0)}%</span></div>)}{!levels.length&&<Empty text="No available price levels"/>}</section>}

createRoot(document.getElementById('root')!).render(<React.StrictMode><App/></React.StrictMode>)
