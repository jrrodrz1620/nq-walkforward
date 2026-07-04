import { useState, useCallback, useMemo, useRef } from "react";
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ReferenceLine, ResponsiveContainer, BarChart, Bar, Cell } from "recharts";
import { parseTV, groupByDay, calcStats } from "./parse.js";

// ─── FIRM CONFIGS ─────────────────────────────────────────────────────────────
const FIRMS = {
  apex:    { name:"Apex Trader Funding", abbr:"Apex",    color:"#f97316", ddType:"EOD",      consistencyDuringEval:false, profitSplit:0.90, firstTierProfit:25000, firstTierSplit:1.0, payoutMinDays:8,  bufferDays:0,  accounts:[{label:"$25K",target:1500,evalDD:1000,fundedDD:1000,dailyLoss:null,minDays:7,evalConsistency:null,fundedConsistency:0.50,evalFee:147,activationFee:85},{label:"$50K",target:3000,evalDD:2500,fundedDD:2500,dailyLoss:null,minDays:7,evalConsistency:null,fundedConsistency:0.50,evalFee:167,activationFee:85},{label:"$100K",target:6000,evalDD:3000,fundedDD:3000,dailyLoss:null,minDays:7,evalConsistency:null,fundedConsistency:0.50,evalFee:207,activationFee:100},{label:"$150K",target:9000,evalDD:4500,fundedDD:4500,dailyLoss:null,minDays:7,evalConsistency:null,fundedConsistency:0.50,evalFee:297,activationFee:130}] },
  topstep: { name:"TopStep",             abbr:"TopStep", color:"#3b82f6", ddType:"EOD",      consistencyDuringEval:true,  profitSplit:0.90, firstTierProfit:0,     firstTierSplit:0.90,payoutMinDays:5,  bufferDays:0,  accounts:[{label:"$50K",target:3000,evalDD:2000,fundedDD:2000,dailyLoss:1000,minDays:1,evalConsistency:0.50,fundedConsistency:0.50,evalFee:109,activationFee:149},{label:"$100K",target:6000,evalDD:3000,fundedDD:3000,dailyLoss:2000,minDays:1,evalConsistency:0.50,fundedConsistency:0.50,evalFee:159,activationFee:149},{label:"$150K",target:9000,evalDD:4500,fundedDD:4500,dailyLoss:3000,minDays:1,evalConsistency:0.50,fundedConsistency:0.50,evalFee:209,activationFee:149}] },
  lucid:   { name:"Lucid Trading",        abbr:"Lucid",   color:"#8b5cf6", ddType:"EOD",      consistencyDuringEval:true,  profitSplit:0.90, firstTierProfit:0,     firstTierSplit:0.90,payoutMinDays:3,  bufferDays:0,  accounts:[
    {label:"Flex $25K", target:1500,evalDD:1000,fundedDD:1000,dailyLoss:null,minDays:1,evalConsistency:0.50,fundedConsistency:null, evalFee:97, activationFee:0},
    {label:"Flex $50K", target:3000,evalDD:2000,fundedDD:2000,dailyLoss:null,minDays:1,evalConsistency:0.50,fundedConsistency:null, evalFee:175,activationFee:0},
    {label:"Flex $100K",target:6000,evalDD:3000,fundedDD:3000,dailyLoss:null,minDays:1,evalConsistency:0.50,fundedConsistency:null, evalFee:275,activationFee:0},
    {label:"Pro $25K",  target:1500,evalDD:1000,fundedDD:1000,dailyLoss:null,minDays:1,evalConsistency:null,fundedConsistency:0.40,evalFee:75, activationFee:0},
    {label:"Pro $50K",  target:3000,evalDD:2000,fundedDD:2000,dailyLoss:null,minDays:1,evalConsistency:null,fundedConsistency:0.40,evalFee:130,activationFee:0},
    {label:"Pro $100K", target:6000,evalDD:3000,fundedDD:3000,dailyLoss:null,minDays:1,evalConsistency:null,fundedConsistency:0.40,evalFee:207,activationFee:0},
  ], note:"EOD trailing DD. No daily loss limit on any account. LucidFlex: 50% consistency during eval only — fully removed in funded (most forgiving funded ruleset in the industry). LucidPro: no eval consistency, 40% consistency in funded, payouts every 3 days. No activation fees. 90/10 split. Transitions to LucidLive after 6 payouts (Flex) or 4 payouts (Pro)." },
  blusky:  { name:"BluSky Trading",      abbr:"BluSky",  color:"#06b6d4", ddType:"Trailing", consistencyDuringEval:true,  profitSplit:0.90, firstTierProfit:0,     firstTierSplit:0.90,payoutMinDays:8,  bufferDays:30, weeklyPayoutCap:3000, accounts:[{label:"$25K",target:1500,evalDD:1000,fundedDD:1000,dailyLoss:500,minDays:8,evalConsistency:0.30,fundedConsistency:null,evalFee:105,activationFee:0},{label:"$50K",target:3000,evalDD:2000,fundedDD:2000,dailyLoss:1000,minDays:8,evalConsistency:0.30,fundedConsistency:null,evalFee:165,activationFee:0},{label:"$100K",target:6000,evalDD:3000,fundedDD:3000,dailyLoss:2000,minDays:8,evalConsistency:0.30,fundedConsistency:null,evalFee:250,activationFee:0}] },
};

const f$ = (v,abs) => { const n=abs?Math.abs(v):v; return (n<0?"-$":"$")+Math.abs(n).toLocaleString("en-US",{maximumFractionDigits:0}); };
const fPct = v => `${(v*100).toFixed(1)}%`;
let _id=0; const uid=()=>String(++_id);

// ─── SIMULATION ───────────────────────────────────────────────────────────────
function simulateEval(daily,firm,acct){
  const{target,evalDD,dailyLoss,minDays,evalConsistency}=acct;
  let bal=0,peak=0,bestDay=0,effTarget=target,passDay=null,failReason=null;
  const rows=[];
  for(let i=0;i<daily.length;i++){
    const{pnl,date}=daily[i];const day=i+1;
    if(dailyLoss!==null&&pnl<-dailyLoss){failReason=`Daily loss limit breached: ${f$(pnl)}`;rows.push({day,date,pnl:Math.round(pnl),balance:Math.round(bal),floor:Math.round(peak-evalDD),target:Math.round(effTarget),event:"FAIL"});break;}
    bal+=pnl;if(bal>peak)peak=bal;
    const floor=peak-evalDD;
    if(bal<floor){failReason=`DD breached: balance ${f$(bal)}, floor ${f$(floor)}`;rows.push({day,date,pnl:Math.round(pnl),balance:Math.round(bal),floor:Math.round(floor),target:Math.round(effTarget),event:"FAIL"});break;}
    if(pnl>bestDay)bestDay=pnl;
    if(firm.consistencyDuringEval&&evalConsistency&&firm.name.includes("BluSky")&&bestDay>effTarget*evalConsistency) effTarget=bestDay/evalConsistency;
    const consistency=firm.consistencyDuringEval&&evalConsistency&&!firm.name.includes("BluSky")&&bal>0?bestDay/bal:0;
    const canPass=bal>=effTarget&&day>=minDays&&!(firm.consistencyDuringEval&&evalConsistency&&!firm.name.includes("BluSky")&&consistency>evalConsistency);
    rows.push({day,date,pnl:Math.round(pnl),balance:Math.round(bal),floor:Math.round(floor),target:Math.round(effTarget),distToTarget:Math.round(effTarget-bal),distToFloor:Math.round(bal-floor),event:canPass?"PASS":null});
    if(canPass){passDay=day;break;}
  }
  if(!passDay&&!failReason) failReason=`Ran out of data after ${rows.length} days.`;
  return{passDay,failReason,balance:bal,rows,effTarget};
}

function simulateFunded(daily,firm,acct){
  const{fundedDD,activationFee=0,evalFee}=acct;
  const{profitSplit,firstTierProfit,firstTierSplit,payoutMinDays=8,weeklyPayoutCap,bufferDays=0}=firm;
  const totalCost=evalFee+activationFee;
  const pool=[]; while(pool.length<252) pool.push(...daily);
  let bal=0,peak=0,floorLocked=false,lockedFloor=null,cumPnL=0,grossExtracted=0,netExtracted=0,cumulativeSplit=0,tradingDays=0,winDays=0,lastPayoutDay=0;
  const rows=[],payouts=[];let blowDay=null,blowReason=null;
  for(let i=0;i<252;i++){
    const{pnl,date}=pool[i];tradingDays=i+1;
    bal+=pnl;cumPnL+=pnl;if(pnl>0)winDays++;if(bal>peak)peak=bal;
    let floor;
    if(floorLocked){floor=lockedFloor;}
    else{floor=peak-fundedDD;if(floor>=-100){floorLocked=true;lockedFloor=Math.max(floor,-100);floor=lockedFloor;}}
    if(bal<floor){blowDay=tradingDays;blowReason=`Balance ${f$(bal)} < floor ${f$(floor)}`;rows.push({day:tradingDays,date,pnl:Math.round(pnl),balance:Math.round(bal),floor:Math.round(floor),cumPayout:Math.round(grossExtracted),netLifetime:Math.round(netExtracted-totalCost),event:"BLOW"});break;}
    const sinceLastPayout=tradingDays-lastPayoutDay;
    const inBuffer=tradingDays<=bufferDays;
    const consistent=!acct.fundedConsistency||cumPnL<=0||(pnl/cumPnL)<=acct.fundedConsistency;
    let payoutEvent=null;
    if(!inBuffer&&cumPnL>0&&sinceLastPayout>=payoutMinDays&&winDays>=Math.min(5,payoutMinDays)&&consistent){
      let avail=cumPnL-grossExtracted;
      if(avail>200){
        if(weeklyPayoutCap) avail=Math.min(avail,weeklyPayoutCap);
        let share;
        if(firstTierProfit>0&&cumulativeSplit<firstTierProfit){const inF=Math.min(avail,firstTierProfit-cumulativeSplit);share=inF*firstTierSplit+Math.max(0,avail-inF)*profitSplit;}
        else share=avail*profitSplit;
        share=Math.round(share);grossExtracted+=avail;netExtracted+=share;cumulativeSplit+=avail;lastPayoutDay=tradingDays;
        payoutEvent={day:tradingDays,date,gross:Math.round(avail),traderShare:share,cumNet:Math.round(netExtracted)};
        payouts.push(payoutEvent);
      }
    }
    rows.push({day:tradingDays,date,pnl:Math.round(pnl),balance:Math.round(bal),floor:Math.round(floor),cumPayout:Math.round(grossExtracted),netLifetime:Math.round(netExtracted-totalCost),event:payoutEvent?"PAYOUT":inBuffer?"BUFFER":null});
  }
  return{rows,payouts,blowDay,blowReason,totalCost,netExtracted:Math.round(netExtracted),finalNetLifetime:Math.round(netExtracted-totalCost)};
}

function runMC(daily,firm,acct,N=1000){
  if(daily.length<5) return null;
  const pnls=daily.map(d=>d.pnl),results=[];
  for(let i=0;i<N;i++){
    const s=[...pnls];for(let j=s.length-1;j>0;j--){const k=Math.floor(Math.random()*(j+1));[s[j],s[k]]=[s[k],s[j]];}
    results.push(simulateEval(s.slice(0,90).map((pnl,i)=>({date:`d${i}`,pnl})),firm,acct).passDay);
  }
  const passed=results.filter(Boolean).sort((a,b)=>a-b);
  const bins={};for(const d of passed)bins[d]=(bins[d]||0)+1;
  return{passRate:passed.length/N,p10:passed[Math.floor(passed.length*.1)]??null,p50:passed[Math.floor(passed.length*.5)]??null,p90:passed[Math.floor(passed.length*.9)]??null,histogram:Object.entries(bins).map(([day,count])=>({day:+day,count})).sort((a,b)=>a.day-b.day)};
}

function runAccountSim(pa, daily) {
  const firm = FIRMS[pa.firmKey];
  const acct = firm.accounts[Math.min(pa.acctIdx, firm.accounts.length-1)];
  const filtered = pa.purchaseDate ? daily.filter(d=>d.date>=pa.purchaseDate) : daily;
  if(!filtered.length) return {...pa, firm, acct, filtered:[], error:"No trades on or after this date."};
  const evalRes = simulateEval(filtered, firm, acct);
  const fundedStart = evalRes.passDay ? filtered.slice(evalRes.passDay) : filtered;
  const fundedRes = simulateFunded(fundedStart.length ? fundedStart : filtered, firm, acct);
  const mcRes = runMC(filtered, firm, acct, 1000);
  return {...pa, firm, acct, filtered, evalRes, fundedRes, mcRes, label: pa.label||(firm.abbr+" "+acct.label)};
}

// ─── APP ──────────────────────────────────────────────────────────────────────
const mkAccount = (overrides={}) => ({id:uid(), firmKey:"apex", acctIdx:1, purchaseDate:"", label:"", ...overrides});

export default function App(){
  const [portfolio, setPortfolio] = useState([mkAccount()]);
  const [daily, setDaily] = useState([]);
  const [tradeCount, setTradeCount] = useState(0);
  const [results, setResults] = useState(null);
  const [selectedId, setSelectedId] = useState(null);
  const [csvErr, setCsvErr] = useState(null);
  const [dragging, setDragging] = useState(false);
  const [addFromDate, setAddFromDate] = useState(null); // {date, x, y} for popover
  const resultsRef = useRef(null);

  const handleFile = useCallback(file=>{
    setCsvErr(null);
    const reader=new FileReader();
    reader.onload=e=>{
      try{
        let text=e.target.result.replace(/^\uFEFF/,"");
        const lines=text.trim().split(/\r?\n/);
        if(lines.length<2){setCsvErr("CSV appears empty.");return;}
        const sep=lines[0].includes("\t")?"\t":lines[0].includes(";")?";":","
        const split=line=>{const out=[];let cur="",inQ=false;for(let i=0;i<line.length;i++){const c=line[i];if(c==='"'){inQ=!inQ;}else if(c===sep&&!inQ){out.push(cur.trim());cur="";}else{cur+=c;}}out.push(cur.trim());return out.map(v=>v.replace(/^"|"$/g,"").trim());};
        const headers=split(lines[0]);
        const rows=lines.slice(1).filter(l=>l.trim()).map(l=>{const v=split(l);const o={};headers.forEach((h,i)=>{o[h]=v[i]??"";});return o;});
        const trades=parseTV(rows);
        if(!trades.length){setCsvErr(`No trades parsed. Columns: "${headers.join(" | ")}"`);return;}
        const d=groupByDay(trades);
        setDaily(d);setTradeCount(trades.length);setResults(null);
      }catch(err){setCsvErr(`Parse error: ${err.message}`);}
    };
    reader.readAsText(file);
  },[]);

  const onDrop=useCallback(e=>{e.preventDefault();setDragging(false);const f=e.dataTransfer.files[0];if(f)handleFile(f);},[handleFile]);
  const onInput=useCallback(e=>{const f=e.target.files[0];if(f)handleFile(f);},[handleFile]);

  const addAccount = (overrides={}) => {
    const a = mkAccount(overrides);
    setPortfolio(p=>[...p,a]);
    return a.id;
  };
  const removeAccount = id => setPortfolio(p=>p.filter(a=>a.id!==id));
  const updateAccount = (id,patch) => {
    setPortfolio(p=>p.map(a=>a.id===id?{...a,...patch}:a));
    // Re-run sim for just this account if results exist
    if(results){
      setResults(prev=>prev.map(r=>{
        if(r.id!==id) return r;
        const updated={...r,...patch};
        return runAccountSim(updated, daily);
      }));
    }
  };

  const runAnalysis = () => {
    if(!daily.length) return;
    const res = portfolio.map(pa=>runAccountSim(pa,daily));
    setResults(res);
    setSelectedId(res[0]?.id);
    setTimeout(()=>resultsRef.current?.scrollIntoView({behavior:"smooth",block:"start"}),100);
  };

  // When chart is clicked, show "add account from this date" popover
  const handleChartClick = useCallback((chartData, event) => {
    if(!chartData?.activePayload?.length) return;
    const point = chartData.activePayload[0].payload;
    if(point?.date) setAddFromDate(point.date);
  }, []);

  const confirmAddFromDate = (firmKey, acctIdx) => {
    if(!addFromDate) return;
    const newId = addAccount({purchaseDate: addFromDate, firmKey, acctIdx});
    // Run sim immediately and add to results
    if(results){
      const newAcc = mkAccount({id:newId, purchaseDate:addFromDate, firmKey, acctIdx});
      const simmed = runAccountSim(newAcc, daily);
      setResults(prev=>[...prev, simmed]);
      setSelectedId(newId);
    }
    setAddFromDate(null);
  };

  const stats = useMemo(()=>calcStats(daily),[daily]);
  const totalInvested = (results||[]).reduce((s,r)=>s+r.acct.evalFee+(r.acct.activationFee||0),0);
  const totalPayouts  = (results||[]).reduce((s,r)=>s+(r.fundedRes?.netExtracted||0),0);
  const netTotal      = (results||[]).reduce((s,r)=>s+(r.fundedRes?.finalNetLifetime||0),0);

  return (
    <div style={{fontFamily:"'Inter',system-ui,sans-serif",background:"#0f172a",color:"#e2e8f0",minHeight:"100vh",padding:16,boxSizing:"border-box",position:"relative"}} onClick={addFromDate?()=>setAddFromDate(null):undefined}>
      {/* Header */}
      <div style={{marginBottom:14,display:"flex",justifyContent:"space-between",alignItems:"flex-start",flexWrap:"wrap",gap:8}}>
        <div>
          <h1 style={{margin:0,fontSize:18,fontWeight:700,color:"#f1f5f9"}}>Prop Firm Readiness Analyzer</h1>
          <p style={{margin:"2px 0 0",fontSize:11,color:"#64748b"}}>Upload trades · Configure accounts · Simulate eval + payout lifecycle</p>
        </div>
        <div style={{fontSize:10,color:"#475569",textAlign:"right",lineHeight:1.5}}>⚠️ Rules current as of Mar 2026.<br/>Verify on each firm's site.</div>
      </div>

      {/* ── SETUP ── always visible */}
      <div style={{display:"grid",gridTemplateColumns:"1fr 1fr",gap:14,marginBottom:14}}>
        {/* LEFT: CSV */}
        <div>
          <Card title="Trade Data">
            <div onDragOver={e=>{e.preventDefault();setDragging(true);}} onDragLeave={()=>setDragging(false)} onDrop={onDrop} onClick={()=>document.getElementById("csv-in").click()} style={{border:`2px dashed ${dragging?"#f97316":"#334155"}`,borderRadius:8,padding:"20px 12px",textAlign:"center",background:dragging?"#f9731618":"transparent",cursor:"pointer",marginBottom:8}}>
              <div style={{fontSize:24,marginBottom:4}}>📁</div>
              <div style={{fontSize:12,color:"#94a3b8"}}>Drop TradingView CSV or click to browse</div>
              <input id="csv-in" type="file" accept=".csv,.txt" onChange={onInput} style={{display:"none"}}/>
            </div>
            {csvErr&&<p style={{color:"#f87171",fontSize:11,margin:"4px 0",whiteSpace:"pre-wrap"}}>{csvErr}</p>}
            {daily.length>0&&(
              <div style={{padding:"6px 10px",background:"#064e3b",borderRadius:7,color:"#6ee7b7",fontSize:11,marginBottom:8}}>
                ✅ {tradeCount} trades · {daily.length} trading days · {daily[0]?.date} → {daily[daily.length-1]?.date}
              </div>
            )}
            <p style={{margin:0,fontSize:10,color:"#475569"}}>Strategy Tester → List of Trades → Export icon</p>
          </Card>
          {stats&&(
            <Card title="Strategy Health">
              <div style={{display:"grid",gridTemplateColumns:"1fr 1fr",gap:6}}>
                {[["Win Rate",fPct(stats.winRate),stats.winRate>=0.5?"#4ade80":"#fb923c"],["Profit Factor",stats.profitFactor===Infinity?"∞":stats.profitFactor.toFixed(2),stats.profitFactor>=1.5?"#4ade80":"#fb923c"],["Best Day",f$(stats.bestDay),"#4ade80"],["Worst Day",f$(Math.abs(stats.worstDay)),"#f87171"],["Hist. Max DD",f$(stats.maxDD),"#f87171"],["Avg Daily P&L",f$(stats.avgDailyPnL),stats.avgDailyPnL>0?"#4ade80":"#f87171"]].map(([l,v,c])=>(
                  <div key={l} style={{background:"#0f172a",borderRadius:6,padding:"7px 9px"}}>
                    <div style={{fontSize:9,color:"#64748b",marginBottom:1}}>{l}</div>
                    <div style={{fontSize:13,fontWeight:700,color:c}}>{v}</div>
                  </div>
                ))}
              </div>
            </Card>
          )}
        </div>

        {/* RIGHT: Portfolio */}
        <div>
          <Card title="Account Portfolio">
            <div style={{display:"flex",flexDirection:"column",gap:8,marginBottom:8}}>
              {portfolio.map((pa,idx)=>(
                <AccountRow key={pa.id} pa={pa} idx={idx} daily={daily} onUpdate={p=>updateAccount(pa.id,p)} onRemove={()=>removeAccount(pa.id)} canRemove={portfolio.length>1}/>
              ))}
            </div>
            <button onClick={()=>addAccount()} style={{width:"100%",padding:"8px",borderRadius:7,border:"1px dashed #334155",background:"transparent",color:"#64748b",cursor:"pointer",fontSize:11,display:"flex",alignItems:"center",justifyContent:"center",gap:5}}>
              <span style={{fontSize:15}}>+</span> Add Account
            </button>
          </Card>
          <button onClick={runAnalysis} disabled={!daily.length} style={{width:"100%",padding:13,borderRadius:9,border:"none",background:daily.length?"#f97316":"#334155",color:"#fff",fontWeight:700,fontSize:14,cursor:daily.length?"pointer":"not-allowed"}}>
            {results?"Re-run Analysis →":"Run Analysis →"}
          </button>
        </div>
      </div>

      {/* ── RESULTS ── appear inline below setup */}
      {results&&(
        <div ref={resultsRef}>
          <div style={{borderTop:"1px solid #1e293b",paddingTop:14,marginBottom:12}}>
            <div style={{display:"flex",justifyContent:"space-between",alignItems:"center",flexWrap:"wrap",gap:8,marginBottom:12}}>
              <h2 style={{margin:0,fontSize:14,fontWeight:700,color:"#f1f5f9"}}>Results</h2>
              {results.length>1&&(
                <div style={{display:"flex",gap:8,flexWrap:"wrap"}}>
                  {[["Invested",f$(totalInvested),"#fb923c"],["Payouts",f$(totalPayouts),"#4ade80"],["Net P&L",f$(netTotal),netTotal>=0?"#4ade80":"#f87171"]].map(([l,v,c])=>(
                    <div key={l} style={{background:"#1e293b",borderRadius:7,padding:"6px 12px",textAlign:"center"}}>
                      <div style={{fontSize:9,color:"#64748b"}}>{l}</div>
                      <div style={{fontSize:13,fontWeight:700,color:c}}>{v}</div>
                    </div>
                  ))}
                </div>
              )}
            </div>

            {/* Account selector tabs */}
            <div style={{display:"flex",gap:4,marginBottom:14,flexWrap:"wrap"}}>
              {results.map(r=>(
                <button key={r.id} onClick={()=>setSelectedId(r.id)} style={{padding:"6px 12px",borderRadius:8,border:`1px solid ${selectedId===r.id?r.firm.color:"#334155"}`,background:selectedId===r.id?r.firm.color+"22":"transparent",color:selectedId===r.id?r.firm.color:"#64748b",cursor:"pointer",fontSize:11,fontWeight:selectedId===r.id?600:400,display:"flex",alignItems:"center",gap:6}}>
                  <span style={{width:7,height:7,borderRadius:"50%",background:r.firm.color,display:"inline-block",flexShrink:0}}/>
                  {r.label}
                  {r.evalRes?.passDay?<span style={{color:"#4ade80",fontSize:9}}>✓ d{r.evalRes.passDay}</span>:<span style={{color:"#f87171",fontSize:9}}>✗</span>}
                  {r.fundedRes?.blowDay&&<span style={{color:"#f87171",fontSize:9}}>💥d{r.fundedRes.blowDay}</span>}
                </button>
              ))}
            </div>

            {/* Detail for selected account */}
            {(()=>{
              const sel = results.find(r=>r.id===selectedId)||results[0];
              if(!sel) return null;
              if(sel.error) return <div style={{color:"#f87171",padding:16,fontSize:12}}>⚠️ {sel.error}</div>;
              return <AccountResults sel={sel} onChartClick={handleChartClick} daily={daily}/>;
            })()}
          </div>
        </div>
      )}

      {/* ── ADD FROM DATE POPOVER ── */}
      {addFromDate&&(
        <div onClick={e=>e.stopPropagation()} style={{position:"fixed",top:"50%",left:"50%",transform:"translate(-50%,-50%)",background:"#1e293b",border:"1px solid #334155",borderRadius:12,padding:20,zIndex:100,boxShadow:"0 20px 60px #00000080",minWidth:280}}>
          <h3 style={{margin:"0 0 4px",fontSize:13,fontWeight:700,color:"#f1f5f9"}}>Add Account from This Date</h3>
          <p style={{margin:"0 0 14px",fontSize:11,color:"#64748b"}}>Purchase date: <strong style={{color:"#e2e8f0"}}>{addFromDate}</strong></p>
          <AddFromDateForm date={addFromDate} onConfirm={confirmAddFromDate} onCancel={()=>setAddFromDate(null)}/>
        </div>
      )}
      {addFromDate&&<div style={{position:"fixed",inset:0,background:"#00000060",zIndex:99}}/>}
    </div>
  );
}

// ─── ADD FROM DATE FORM ───────────────────────────────────────────────────────
function AddFromDateForm({date, onConfirm, onCancel}){
  const [firmKey,setFirmKey]=useState("apex");
  const [acctIdx,setAcctIdx]=useState(1);
  const firm=FIRMS[firmKey];
  return(
    <div>
      <div style={{display:"grid",gridTemplateColumns:"1fr 1fr",gap:8,marginBottom:14}}>
        <div>
          <div style={{fontSize:10,color:"#64748b",marginBottom:4}}>Firm</div>
          <select value={firmKey} onChange={e=>{setFirmKey(e.target.value);setAcctIdx(0);}} style={{width:"100%",background:"#0f172a",border:"1px solid #334155",borderRadius:6,color:"#e2e8f0",padding:"7px 8px",fontSize:12}}>
            {Object.entries(FIRMS).map(([k,f])=><option key={k} value={k}>{f.name}</option>)}
          </select>
        </div>
        <div>
          <div style={{fontSize:10,color:"#64748b",marginBottom:4}}>Account Size</div>
          <select value={acctIdx} onChange={e=>setAcctIdx(+e.target.value)} style={{width:"100%",background:"#0f172a",border:"1px solid #334155",borderRadius:6,color:"#e2e8f0",padding:"7px 8px",fontSize:12}}>
            {firm.accounts.map((a,i)=><option key={i} value={i}>{a.label} — Eval {f$(a.evalFee)}</option>)}
          </select>
        </div>
      </div>
      <div style={{padding:"8px 10px",background:"#0f172a",borderRadius:7,fontSize:11,color:"#94a3b8",marginBottom:14,lineHeight:1.6}}>
        {firm.abbr} {firm.accounts[acctIdx]?.label} · Target {f$(firm.accounts[acctIdx]?.target)} · DD {f$(firm.accounts[acctIdx]?.evalDD)} · Cost {f$(firm.accounts[acctIdx]?.evalFee+(firm.accounts[acctIdx]?.activationFee||0))}
      </div>
      <div style={{display:"flex",gap:8}}>
        <button onClick={()=>onConfirm(firmKey,acctIdx)} style={{flex:1,padding:"9px",borderRadius:8,border:"none",background:FIRMS[firmKey].color,color:"#fff",fontWeight:700,fontSize:12,cursor:"pointer"}}>
          Add & Simulate →
        </button>
        <button onClick={onCancel} style={{padding:"9px 14px",borderRadius:8,border:"1px solid #334155",background:"transparent",color:"#94a3b8",cursor:"pointer",fontSize:12}}>
          Cancel
        </button>
      </div>
    </div>
  );
}

// ─── ACCOUNT ROW ──────────────────────────────────────────────────────────────
function AccountRow({pa,idx,daily,onUpdate,onRemove,canRemove}){
  const firm=FIRMS[pa.firmKey];
  const acct=firm.accounts[Math.min(pa.acctIdx,firm.accounts.length-1)];
  const filtered=useMemo(()=>pa.purchaseDate?daily.filter(d=>d.date>=pa.purchaseDate):daily,[pa.purchaseDate,daily]);
  const fs=useMemo(()=>calcStats(filtered),[filtered]);
  return(
    <div style={{border:`1px solid #334155`,borderLeft:`3px solid ${firm.color}`,borderRadius:8,padding:10,background:"#0f172a10"}}>
      <div style={{display:"grid",gridTemplateColumns:"auto 1fr 1fr 1fr auto",gap:8,alignItems:"center",marginBottom:filtered.length&&pa.purchaseDate?8:0}}>
        <input value={pa.label} onChange={e=>onUpdate({label:e.target.value})} placeholder={`Account ${idx+1}`} style={{background:"transparent",border:"none",color:"#e2e8f0",fontSize:11,fontWeight:600,outline:"none",width:90}}/>
        <select value={pa.firmKey} onChange={e=>onUpdate({firmKey:e.target.value,acctIdx:0})} style={{background:"#1e293b",border:"1px solid #334155",borderRadius:5,color:"#e2e8f0",padding:"5px 6px",fontSize:11}}>
          {Object.entries(FIRMS).map(([k,f])=><option key={k} value={k}>{f.abbr}</option>)}
        </select>
        <select value={pa.acctIdx} onChange={e=>onUpdate({acctIdx:+e.target.value})} style={{background:"#1e293b",border:"1px solid #334155",borderRadius:5,color:"#e2e8f0",padding:"5px 6px",fontSize:11}}>
          {FIRMS[pa.firmKey].accounts.map((a,i)=><option key={i} value={i}>{a.label}</option>)}
        </select>
        <div>
          <div style={{fontSize:9,color:"#64748b",marginBottom:2}}>Purchase Date</div>
          <input type="date" value={pa.purchaseDate} onChange={e=>onUpdate({purchaseDate:e.target.value})} style={{background:"#1e293b",border:"1px solid #334155",borderRadius:5,color:pa.purchaseDate?"#e2e8f0":"#64748b",padding:"4px 6px",fontSize:11,width:"100%",boxSizing:"border-box"}}/>
        </div>
        {canRemove&&<button onClick={onRemove} style={{background:"none",border:"none",color:"#475569",cursor:"pointer",fontSize:18,lineHeight:1,padding:"0 2px",alignSelf:"flex-start"}}>×</button>}
      </div>
      {pa.purchaseDate&&filtered.length>0&&fs&&(
        <div style={{display:"flex",gap:5,flexWrap:"wrap"}}>
          <Tag label="Days" value={filtered.length} color="#94a3b8"/>
          <Tag label="Avg/day" value={f$(fs.avgDailyPnL)} color={fs.avgDailyPnL>0?"#4ade80":"#f87171"}/>
          <Tag label="Max DD" value={f$(fs.maxDD)} color={fs.maxDD<=acct.evalDD?"#4ade80":"#f87171"}/>
          <Tag label="Cost" value={f$(acct.evalFee+(acct.activationFee||0))} color={firm.color}/>
        </div>
      )}
      {pa.purchaseDate&&!filtered.length&&<div style={{fontSize:10,color:"#f87171",marginTop:4}}>⚠ No trades found on or after this date</div>}
      {!pa.purchaseDate&&<div style={{fontSize:10,color:"#64748b",marginTop:4,fontStyle:"italic"}}>No date set — using full dataset. Click a point on a results chart to set a date.</div>}
    </div>
  );
}

// ─── ACCOUNT RESULTS ─────────────────────────────────────────────────────────
function AccountResults({sel, onChartClick}){
  const [subTab,setSubTab]=useState("lifecycle");
  const C=sel.firm.color;
  const{evalRes,fundedRes,mcRes,firm,acct,filtered}=sel;
  return(
    <div>
      <div style={{display:"flex",gap:3,marginBottom:12,borderBottom:"1px solid #1e293b",alignItems:"center"}}>
        {[["lifecycle","💰 Lifecycle"],["eval","📋 Eval Day-by-Day"],["monte","🎲 Monte Carlo"]].map(([t,l])=>(
          <button key={t} onClick={()=>setSubTab(t)} style={{padding:"5px 12px",border:"none",background:"none",cursor:"pointer",color:subTab===t?C:"#64748b",borderBottom:`2px solid ${subTab===t?C:"transparent"}`,fontSize:11,fontWeight:subTab===t?600:400}}>{l}</button>
        ))}
        <span style={{marginLeft:"auto",fontSize:10,color:"#64748b",paddingRight:4}}>
          {filtered.length} days {sel.purchaseDate?`· from ${sel.purchaseDate}`:"· full dataset"}
          {subTab==="lifecycle"&&<span style={{color:"#475569",marginLeft:6}}>· click chart to add account from that date</span>}
        </span>
      </div>
      {subTab==="lifecycle"&&fundedRes&&<LifecycleTab firm={firm} acct={acct} evalRes={evalRes} fundedRes={fundedRes} C={C} onChartClick={onChartClick}/>}
      {subTab==="eval"&&evalRes&&<EvalTab evalRes={evalRes} C={C}/>}
      {subTab==="monte"&&<MonteTab mcRes={mcRes} dailyLen={filtered.length} C={C}/>}
    </div>
  );
}

// ─── LIFECYCLE TAB ────────────────────────────────────────────────────────────
function LifecycleTab({firm,acct,evalRes,fundedRes,C,onChartClick}){
  const{rows,payouts,blowDay,blowReason,totalCost,netExtracted,finalNetLifetime}=fundedRes;
  const CustomDot=props=>{
    const{cx,cy,payload}=props;
    if(payload.event==="PAYOUT") return <circle cx={cx} cy={cy} r={4} fill="#4ade80" stroke="#0f172a" strokeWidth={1}/>;
    if(payload.event==="BLOW")   return <circle cx={cx} cy={cy} r={6} fill="#f87171" stroke="#0f172a" strokeWidth={1}/>;
    return null;
  };
  const be=rows.find(r=>r.netLifetime>=0);
  return(
    <div style={{display:"grid",gridTemplateColumns:"1fr 1fr",gap:12}}>
      {/* Main chart */}
      <Card title={`Funded Lifecycle · ${firm.abbr} ${acct.label} · click any point to add an account from that date`}>
        <ResponsiveContainer width="100%" height={230}>
          <LineChart data={rows} margin={{top:4,right:8,bottom:16,left:0}} onClick={onChartClick} style={{cursor:"crosshair"}}>
            <CartesianGrid strokeDasharray="3 3" stroke="#1e293b"/>
            <XAxis dataKey="day" stroke="#334155" tick={{fontSize:9,fill:"#475569"}} label={{value:"Funded Trading Day",position:"insideBottom",offset:-8,fontSize:9,fill:"#475569"}}/>
            <YAxis stroke="#334155" tick={{fontSize:9,fill:"#475569"}} tickFormatter={v=>`${Math.abs(v)>=1000?(v<0?"-":"")+Math.round(Math.abs(v)/1000)+"k":v}`}/>
            <Tooltip contentStyle={{background:"#1e293b",border:"1px solid #334155",borderRadius:7,fontSize:10}} formatter={(v,n)=>[`$${v?.toLocaleString()}`,n]} labelFormatter={l=>`Day ${l}`}/>
            <ReferenceLine y={0} stroke="#475569" strokeDasharray="2 2"/>
            <Line type="monotone" dataKey="balance" stroke={C} strokeWidth={2} dot={<CustomDot/>} name="Balance"/>
            <Line type="monotone" dataKey="floor" stroke="#f87171" strokeWidth={1.5} strokeDasharray="3 3" dot={false} name="DD Floor"/>
            <Line type="monotone" dataKey="cumPayout" stroke="#4ade80" strokeWidth={1.5} strokeDasharray="4 2" dot={false} name="Cum. Payouts"/>
          </LineChart>
        </ResponsiveContainer>
        <div style={{display:"flex",gap:8,marginTop:5,flexWrap:"wrap",alignItems:"center"}}>
          <Leg color={C} label="Balance"/>
          <Leg color="#f87171" label="DD Floor" dashed/>
          <Leg color="#4ade80" label="Cum. Payouts" dashed/>
          <span style={{fontSize:9,color:"#64748b"}}>🟢 payout &nbsp; 🔴 blow</span>
        </div>
      </Card>

      {/* Net P&L chart */}
      <Card title="Net Lifetime P&L (after fees)">
        <ResponsiveContainer width="100%" height={160}>
          <LineChart data={rows} margin={{top:4,right:8,bottom:16,left:0}} onClick={onChartClick} style={{cursor:"crosshair"}}>
            <CartesianGrid strokeDasharray="3 3" stroke="#1e293b"/>
            <XAxis dataKey="day" stroke="#334155" tick={{fontSize:9,fill:"#475569"}} label={{value:"Funded Trading Day",position:"insideBottom",offset:-8,fontSize:9,fill:"#475569"}}/>
            <YAxis stroke="#334155" tick={{fontSize:9,fill:"#475569"}} tickFormatter={v=>`${Math.abs(v)>=1000?(v<0?"-":"")+Math.round(Math.abs(v)/1000)+"k":v}`}/>
            <Tooltip contentStyle={{background:"#1e293b",border:"1px solid #334155",borderRadius:7,fontSize:10}} formatter={(v,n)=>[`$${v?.toLocaleString()}`,n]} labelFormatter={l=>`Day ${l}`}/>
            <ReferenceLine y={0} stroke="#f87171" strokeDasharray="3 3"/>
            <Line type="stepAfter" dataKey="netLifetime" stroke="#a78bfa" strokeWidth={2} dot={false} name="Net P&L"/>
          </LineChart>
        </ResponsiveContainer>
        <div style={{display:"grid",gridTemplateColumns:"1fr 1fr 1fr",gap:6,marginTop:8}}>
          <Chip label="Total Payouts" value={f$(netExtracted)} color="#4ade80"/>
          <Chip label="Eval Cost"     value={f$(totalCost)}    color="#fb923c"/>
          <Chip label="Net Lifetime"  value={f$(finalNetLifetime)} color={finalNetLifetime>=0?"#4ade80":"#f87171"}/>
        </div>
        {be&&<p style={{margin:"6px 0 0",fontSize:10,color:"#6ee7b7"}}>Break-even at funded day {be.day} (~{Math.ceil(be.day*7/5)} calendar days)</p>}
      </Card>

      {/* Payout table */}
      <Card title="Payout Events">
        {payouts.length>0?(
          <div style={{overflowY:"auto",maxHeight:170}}>
            <table style={{width:"100%",fontSize:11,borderCollapse:"collapse"}}>
              <thead><tr style={{color:"#64748b",borderBottom:"1px solid #334155"}}>
                {["Day","Date","Gross P&L","Your Cut","Running Net"].map((h,i)=><th key={h} style={{padding:"3px 6px",textAlign:i>1?"right":"left",fontWeight:500}}>{h}</th>)}
              </tr></thead>
              <tbody>
                {payouts.map((p,i)=>(
                  <tr key={i} style={{borderBottom:"1px solid #1e293b"}}>
                    <td style={{padding:"3px 6px",color:"#94a3b8"}}>{p.day}</td>
                    <td style={{padding:"3px 6px",color:"#64748b",fontSize:10}}>{p.date}</td>
                    <td style={{padding:"3px 6px",textAlign:"right",color:"#e2e8f0"}}>{f$(p.gross)}</td>
                    <td style={{padding:"3px 6px",textAlign:"right",color:"#4ade80",fontWeight:600}}>{f$(p.traderShare)}</td>
                    <td style={{padding:"3px 6px",textAlign:"right",color:p.cumNet-totalCost>=0?"#4ade80":"#fb923c"}}>{f$(p.cumNet-totalCost)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ):<p style={{color:"#64748b",fontSize:11}}>No payouts triggered.</p>}
      </Card>

      {/* Blow / survival */}
      <Card title="Account Outcome">
        {blowDay?(
          <>
            <Alert type="err">Blown on funded day {blowDay} — {blowReason}</Alert>
            <p style={{margin:"8px 0 0",fontSize:11,color:"#cbd5e1",lineHeight:1.7}}>
              {payouts.length} payout{payouts.length!==1?"s":""} extracted totalling <strong style={{color:"#4ade80"}}>{f$(netExtracted)}</strong> before the blow.<br/>
              Net after fees: <strong style={{color:finalNetLifetime>=0?"#4ade80":"#f87171"}}>{f$(finalNetLifetime)}</strong>. {finalNetLifetime>=0?"✅ Profitable overall.":"⚠️ Blew before recouping eval fee — take payouts more aggressively or reduce size."}
            </p>
          </>
        ):<Alert type="ok">Survived full 252-day simulation. Net: {f$(finalNetLifetime)}.</Alert>}
        {evalRes&&(
          <div style={{marginTop:8,padding:"7px 10px",background:"#0f172a",borderRadius:7,fontSize:11,color:"#94a3b8",lineHeight:1.6}}>
            Eval: {evalRes.passDay?<span style={{color:"#4ade80"}}>passed day {evalRes.passDay} (balance {f$(evalRes.balance)})</span>:<span style={{color:"#f87171"}}>{evalRes.failReason}</span>}
          </div>
        )}
      </Card>
    </div>
  );
}

// ─── EVAL TAB ─────────────────────────────────────────────────────────────────
function EvalTab({evalRes,C}){
  const{rows,passDay,failReason,effTarget}=evalRes;
  return(
    <div>
      <div style={{padding:"8px 12px",borderRadius:7,marginBottom:10,background:passDay?"#064e3b":"#450a0a",border:`1px solid ${passDay?"#065f46":"#7f1d1d"}`,fontSize:11,color:passDay?"#6ee7b7":"#fca5a5"}}>
        {passDay?`✅ Passed day ${passDay} · balance ${f$(rows.find(r=>r.event==="PASS")?.balance??0)} vs ${f$(effTarget)} target`:`❌ ${failReason}`}
      </div>
      <Card title="Day-by-Day">
        <div style={{overflowY:"auto",maxHeight:420}}>
          <table style={{width:"100%",fontSize:11,borderCollapse:"collapse",tableLayout:"fixed"}}>
            <thead style={{position:"sticky",top:0,background:"#1e293b",zIndex:1}}>
              <tr style={{color:"#64748b",borderBottom:"1px solid #334155"}}>
                {["Day","Date","P&L","Balance","DD Floor","→ Target","→ Floor",""].map((h,i)=>(
                  <th key={i} style={{padding:"4px 7px",textAlign:i>1&&i<7?"right":"left",fontWeight:500}}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((r,i)=>(
                <tr key={i} style={{borderBottom:"1px solid #1e293b",background:r.event==="PASS"?"#064e3b22":r.event==="FAIL"?"#450a0a22":"transparent"}}>
                  <td style={{padding:"4px 7px",color:"#94a3b8"}}>{r.day}</td>
                  <td style={{padding:"4px 7px",color:"#64748b",fontSize:10}}>{r.date}</td>
                  <td style={{padding:"4px 7px",textAlign:"right",color:r.pnl>=0?"#4ade80":"#f87171",fontWeight:500}}>{f$(r.pnl)}</td>
                  <td style={{padding:"4px 7px",textAlign:"right"}}>{f$(r.balance)}</td>
                  <td style={{padding:"4px 7px",textAlign:"right",color:"#f87171"}}>{f$(r.floor)}</td>
                  <td style={{padding:"4px 7px",textAlign:"right",color:r.distToTarget<=0?"#4ade80":"#94a3b8"}}>{f$(r.distToTarget)}</td>
                  <td style={{padding:"4px 7px",textAlign:"right",color:r.distToFloor<500?"#f87171":r.distToFloor<1200?"#fb923c":"#64748b"}}>{f$(r.distToFloor)}</td>
                  <td style={{padding:"4px 7px",textAlign:"center"}}>{r.event==="PASS"&&<Badge color="#6ee7b7" bg="#065f46">PASS</Badge>}{r.event==="FAIL"&&<Badge color="#fca5a5" bg="#7f1d1d">FAIL</Badge>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  );
}

// ─── MONTE TAB ────────────────────────────────────────────────────────────────
function MonteTab({mcRes,dailyLen,C}){
  if(!mcRes) return <p style={{color:"#64748b",padding:20}}>Not enough data (need 5+ days).</p>;
  return(
    <div style={{display:"grid",gridTemplateColumns:"1fr 1fr",gap:12}}>
      <Card title="Days to Pass Distribution">
        <div style={{display:"flex",gap:7,marginBottom:10,flexWrap:"wrap"}}>
          {[["Pass Rate",fPct(mcRes.passRate),mcRes.passRate>0.65?"#4ade80":mcRes.passRate>0.35?"#fb923c":"#f87171"],["Best 10%",mcRes.p10?`${mcRes.p10}d`:"—","#4ade80"],["Median",mcRes.p50?`${mcRes.p50}d`:"—","#e2e8f0"],["Worst 10%",mcRes.p90?`${mcRes.p90}d`:"—","#fb923c"]].map(([l,v,c])=>(
            <div key={l} style={{flex:1,minWidth:55,background:"#0f172a",borderRadius:7,padding:"7px 9px",textAlign:"center"}}>
              <div style={{fontSize:9,color:"#64748b"}}>{l}</div><div style={{fontSize:16,fontWeight:700,color:c}}>{v}</div>
            </div>
          ))}
        </div>
        <ResponsiveContainer width="100%" height={170}>
          <BarChart data={mcRes.histogram} margin={{top:4,right:8,bottom:16,left:0}}>
            <CartesianGrid strokeDasharray="3 3" stroke="#1e293b"/>
            <XAxis dataKey="day" stroke="#334155" tick={{fontSize:9,fill:"#475569"}} label={{value:"Days to Pass",position:"insideBottom",offset:-8,fontSize:9,fill:"#475569"}}/>
            <YAxis stroke="#334155" tick={{fontSize:9,fill:"#475569"}}/>
            <Tooltip contentStyle={{background:"#1e293b",border:"1px solid #334155",borderRadius:7,fontSize:10}} formatter={v=>[`${v} runs`,"Count"]} labelFormatter={l=>`${l} days`}/>
            <Bar dataKey="count" radius={[2,2,0,0]}>{mcRes.histogram.map((_,i)=><Cell key={i} fill={C} opacity={0.8}/>)}</Bar>
          </BarChart>
        </ResponsiveContainer>
        {dailyLen<30&&<p style={{margin:"6px 0 0",fontSize:10,color:"#854d0e"}}>⚠️ Only {dailyLen} days in filtered data — 30+ recommended.</p>}
      </Card>
      <Card title="What This Means">
        <div style={{fontSize:11,color:"#cbd5e1",lineHeight:1.8}}>
          {mcRes.passRate>0.65&&<p style={{margin:"0 0 8px",color:"#6ee7b7"}}>✅ Strong pass rate across 1,000 randomised trade sequences.</p>}
          {mcRes.passRate<=0.65&&mcRes.passRate>0.35&&<p style={{margin:"0 0 8px",color:"#fbbf24"}}>⚡ Moderate — sequence risk is real. One bad opening streak can blow the eval.</p>}
          {mcRes.passRate<=0.35&&<p style={{margin:"0 0 8px",color:"#f87171"}}>⚠️ Low pass rate — size down or try a larger account tier with more buffer.</p>}
          {mcRes.p50&&<p style={{margin:"0 0 6px"}}>Median: <strong>{mcRes.p50} trading days</strong> (~{Math.ceil(mcRes.p50*7/5)} calendar days).</p>}
          <p style={{margin:"8px 0 0",fontSize:10,color:"#64748b"}}>Each of 1,000 runs shuffles your filtered daily P&Ls and re-simulates the eval from scratch.</p>
        </div>
      </Card>
    </div>
  );
}

// ─── SHARED ───────────────────────────────────────────────────────────────────
function Card({title,children}){return(<div style={{background:"#1e293b22",border:"1px solid #1e293b",borderRadius:10,padding:12,marginBottom:10}}><h3 style={{margin:"0 0 8px",fontSize:10,fontWeight:600,color:"#64748b",textTransform:"uppercase",letterSpacing:"0.06em"}}>{title}</h3>{children}</div>);}
function Chip({label,value,color}){return(<div style={{flex:1,background:"#0f172a",borderRadius:7,padding:"6px 9px"}}><div style={{fontSize:9,color:"#64748b"}}>{label}</div><div style={{fontSize:13,fontWeight:700,color}}>{value}</div></div>);}
function Tag({label,value,color}){return(<span style={{fontSize:10,padding:"2px 7px",borderRadius:6,background:color+"18",color,border:`1px solid ${color}33`}}>{label}: <strong>{value}</strong></span>);}
function Leg({color,label,dashed}){return(<div style={{display:"flex",alignItems:"center",gap:5,fontSize:10,color:"#94a3b8"}}><div style={{width:16,height:2,background:dashed?"none":color,borderTop:dashed?`2px dashed ${color}`:"none"}}/>{label}</div>);}
function Alert({type,children}){const m={err:["#450a0a","#f87171"],warn:["#431407","#fdba74"],ok:["#064e3b","#6ee7b7"]};const[bg,c]=m[type]||m.warn;return(<div style={{padding:"7px 10px",background:bg,borderRadius:7,fontSize:11,color:c,lineHeight:1.6,marginBottom:6}}>{children}</div>);}
function Badge({color,bg,children}){return(<span style={{background:bg,color,padding:"1px 6px",borderRadius:9,fontSize:9,fontWeight:700}}>{children}</span>);}
