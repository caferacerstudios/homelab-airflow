import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { collectTeam, parseRoster, parseTransactions, parseInjuries, reconcileRoster, reconcileTransactions, reconcileInjuries } from '../collector.mjs';

const sources = JSON.parse(fs.readFileSync(new URL('../teams.json', import.meta.url)));
const names = {seahawks:['Seattle','Seahawks'],broncos:['Denver','Broncos'],packers:['Green Bay','Packers'],vikings:['Minnesota','Vikings'],chiefs:['Kansas City','Chiefs'],patriots:['New England','Patriots']};
const siteFor = slug => ({slug,city:names[slug][0],name:names[slug][1],abbreviation:sources[slug].abbreviation});
const teamFor = slug => ({...sources[slug],rosterUrl:`https://${sources[slug].domain}/team/players-roster/`,injuriesUrl:`https://${sources[slug].domain}/team/injury-report/`,transactionsUrl:`https://${sources[slug].domain}/team/transactions/2026`});
const now = '2026-09-12T20:00:00.000Z';
const title = team => `<title>${team.name} Team Information</title>`;
const rosterSection = (status, name='Test Player', sourceId='test-player') => `<div class="nfl-o-roster"><h4><span class="nfl-o-roster__title-status">${status}</span></h4><table><tr><td><a href="/team/players-roster/${sourceId}/">${name}</a></td><td>12</td><td>QB</td></tr></table></div>`;
const rosterHtml = team => title(team) + Array.from({length:50},(_,i)=>rosterSection('Active',`Player Number ${i}`,`player-number-${i}`)).join('');
const transactionsHtml = (team,copy='Signed QB Player Number 1. Released RB Former Player.',year=2026) => `${title(team)}<select><option value="/team/transactions/${year}" selected>${year}</option></select><div class="nfl-c-transactions-report"><table><thead><tr><th class="nfl-c-transactions-report__month">September</th></tr></thead><tbody><tr><td class="nfl-c-transactions-report__date">09/10</td><td>${copy}</td></tr></tbody></table></div>`;
function injuryTable(name,player='Test Player',sourceId='test-player',statuses=['LP','FP','FP','(-)'],days=['Wed','Thu','Fri']) {
  return `<div class="nfl-o-injury-report__club-name">unused</div><span class="nfl-o-injury-report__club-name">${name}</span><table><caption>Table - Injury report</caption><thead><tr>${['Player','Position','Injury',...days,'Game Status'].map(x=>`<th>${x}</th>`).join('')}</tr></thead><tbody><tr><td><a href="/team/players-roster/${sourceId}/">${player}</a></td><td>QB</td><td>Knee</td>${statuses.map(x=>`<td>${x}</td>`).join('')}</tr></tbody></table>`;
}
const injuryHtml = team => `${title(team)}<select><option value="/team/injury-report/week/REG-1" selected>Week 1</option></select>${injuryTable('Opponent Team','Opponent Player','opponent-player')}${injuryTable(team.name)}`;
const context = slug => ({team:teamFor(slug),site:siteFor(slug),now,season:2026,nfl:{schedule:{season:2026,gamesRegular:[{id:1,week:1,date:'2026-09-13'}],playerDirectory:[]}}});
const request = slug => ({site:siteFor(slug),now,runId:'fixture-run',previous:{roster:null,injuries:null,transactions:null},nfl:context(slug).nfl});
function fetcher(team, overrides={}) {
  return async url => ({ok:true,status:200,text:async()=> overrides[url] ?? (url===team.rosterUrl?rosterHtml(team):url===team.injuriesUrl?injuryHtml(team):transactionsHtml(team))});
}

test('all six teams collect correctly tagged independent artifacts with fixed official sources', async()=>{
  for (const slug of Object.keys(sources)) {
    const result=await collectTeam(request(slug),{fetchImpl:fetcher(teamFor(slug))});
    for (const name of ['roster','injuries','transactions']) assert.equal(result[name].team,slug);
    assert.equal(result.roster.players.length,50);
    assert.equal(result.injuries.records.length,3);
    assert(result.injuries.records.every(row=>row.playerName==='Test Player'));
    assert.equal(result.report.counts.currentRoster,50);
  }
});
test('unknown or mismatched team configuration never makes an HTTP request',async()=>{
  let calls=0;
  const bad={...request('broncos'),site:{...siteFor('broncos'),city:'Seattle'}};
  await assert.rejects(collectTeam(bad,{fetchImpl:()=>{calls++;}}),/identity/);
  assert.equal(calls,0);
});
test('all official roster sections retain membership and their precise original label',()=>{
  const team=teamFor('vikings');
  const labels=['Active','Reserve/Injured','Reserve/Injured; Designated for Return','Reserve/Designated to Return','Reserve/Physically Unable to Perform','Practice Squad','Practice Squad/International','Commissioner Exempt','Reserve/Suspended by Commissioner','Reserve/Non-Football Injury'];
  const rows=parseRoster(title(team)+labels.map((s,i)=>rosterSection(s,`Player ${i}`,`player-${i}`)).join(''),team);
  assert.equal(rows.length,labels.length);
  assert.deepEqual(rows.map(p=>p.sourceStatus),labels);
  assert.equal(rows[8].status,'Suspended'); assert.equal(rows[9].status,'Reserve/Non-Football Injury');
});
test('new official sections fail instead of silently dropping reserve players',()=>{
  const team=teamFor('broncos');
  assert.throws(()=>parseRoster(title(team)+rosterSection('Reserve/Unknown'),team),/Unrecognized official roster section/);
});
test('roster source must belong to the intended club',()=>{
  assert.throws(()=>parseRoster(rosterHtml(teamFor('seahawks')),teamFor('chiefs')),/did not identify/);
});
test('stable IDs and metadata persist while departing players become historical',()=>{
  const ctx=context('seahawks');
  const rows=parseRoster(rosterHtml(ctx.team),ctx.team);
  const prior={players:[{id:'legacy-profile',name:rows[0].name,status:'Active',custom:'keep',legacyIds:['old-profile']},{id:'departed',name:'Former Player',status:'Active'}]};
  const next=reconcileRoster(prior,rows,ctx);
  assert.equal(next.players[0].id,'legacy-profile'); assert.equal(next.players[0].custom,'keep');
  assert.deepEqual(next.players[0].legacyIds,['old-profile']);assert.equal(next.players.at(-1).status,'Historical');
});
test('NFL ID enrichment requires a unique exact name and position match and never determines membership',()=>{
  const ctx=context('packers'),rows=parseRoster(rosterHtml(ctx.team),ctx.team);
  ctx.nfl.schedule.playerDirectory=[{id:100,full_name:rows[0].name,position_abbreviation:'QB'},{id:101,full_name:rows[1].name,position_abbreviation:'WR'},{id:102,full_name:rows[2].name,position_abbreviation:'QB'},{id:103,full_name:rows[2].name,position_abbreviation:'QB'},{id:104,full_name:'Former Player',position_abbreviation:'QB'}];
  const next=reconcileRoster(null,rows,ctx);
  assert.equal(next.players[0].balldontlieId,100);assert.equal(next.players[1].balldontlieId,undefined);assert.equal(next.players[2].balldontlieId,undefined);assert.equal(next.players.length,50);
});
test('count collapse preserves prior data by failing candidate generation',()=>{
  const ctx=context('seahawks'),rows=parseRoster(rosterHtml(ctx.team),ctx.team);
  const prior={players:Array.from({length:60},(_,i)=>({id:`id-${i}`,name:`Old ${i}`,status:'Active'}))};
  assert.throws(()=>reconcileRoster(prior,rows,ctx),/Large roster count change/);
});
test('complete official transaction rows include all grammatical forms without invented status moves',()=>{
  const ctx=context('chiefs'),roster={players:[]};
  for (const copy of ['QB A traded to another club for a draft pick.','RB A and RB B reached injury settlements.','TE A extended.','S A signed to practice squad; G B waived.','Player A (G) Practice Squad contract terminated.','Signed Player A from practice squad. Elevated Player B.','RB A reverted to practice squad.']) {
    const rows=parseTransactions(transactionsHtml(ctx.team,copy),{...ctx,roster});
    assert.equal(rows.length,1);assert.equal(rows[0].description,copy);assert.equal(rows[0].entityType,'transaction');assert.equal(rows[0].newStatus,null);assert.equal(rows[0].previousStatus,null);assert.equal(rows[0].datePrecision,'day');
  }
});
test('transaction season is confirmed from the source rather than inferred after a redirect',()=>{
  const ctx=context('chiefs');
  assert.throws(()=>parseTransactions(transactionsHtml(ctx.team,'Signed Player',2025),{...ctx,roster:{players:[]}}),/confirm the requested season/);
});
test('transaction publication appends idempotently and retains earlier curated events',()=>{
  const ctx=context('seahawks'),roster={players:[]};
  const fetched=parseTransactions(transactionsHtml(ctx.team),{...ctx,roster});
  const curated={timestamp:'2026-09-08T20:00:00Z',playerId:'legacy',description:'Curated prior event'};
  const next=reconcileTransactions({records:[curated]},fetched,ctx);
  assert.equal(reconcileTransactions(next,fetched,ctx).records.length,2);assert.deepEqual(next.records[0],curated);
});
test('injury parsing selects this team even when the opponent table appears first',()=>{
  const ctx=context('vikings');
  const parsed=parseInjuries(injuryHtml(ctx.team),{...ctx,roster:{players:[]}});
  assert(parsed.records.every(row=>row.playerName==='Test Player')); assert.equal(parsed.records.length,3);
});
test('injury weekday dates anchor to the selected week schedule and never shift with later collection',()=>{
  const ctx=context('seahawks');
  const html=`${title(ctx.team)}<option value="/team/injury-report/week/REG-1" selected>Week 1</option>${injuryTable(ctx.team.name,'Test Player','test-player',['LP','LP','DNP','OUT'],['Sun','Mon','Tue'])}`;
  ctx.nfl.schedule.gamesRegular[0].date='2026-09-09';
  const first=parseInjuries(html,{...ctx,roster:{players:[]}}),later=parseInjuries(html,{...ctx,now:'2026-09-18T20:00:00Z',roster:{players:[]}});
  assert.deepEqual(first.records,later.records);
  assert.deepEqual(first.records.map(x=>x.date.slice(0,10)),['2026-09-06','2026-09-07','2026-09-08','2026-09-08']);
});
test('injury matching reconciles official URL slug with existing canonical player ID',()=>{
  const ctx=context('seahawks');
  const parsed=parseInjuries(injuryHtml(ctx.team),{...ctx,roster:{players:[{id:'custom-player',name:'Test Player',sourceId:'test-player'}]}});
  assert(parsed.records.every(row=>row.playerId==='custom-player'));
});
test('unavailable schedule keeps injury history and does not make it freshly dated',()=>{
  const ctx=context('seahawks');
  const parsed=parseInjuries(injuryHtml(ctx.team),{...ctx,nfl:null,roster:{players:[]}});
  const old={asOf:'2026-09-08T12:00:00Z',records:[{date:'2026-09-08T12:00:00Z',playerId:'legacy',status:'Out'}]};
  const next=reconcileInjuries(old,parsed,ctx);
  assert.equal(next.availability,'unavailable');assert.equal(next.asOf,old.asOf);assert.deepEqual(next.records,old.records);assert.equal(next.sourceCheckedAt,now);
  assert.equal(reconcileInjuries(null,parsed,ctx).asOf,null);
});
test('stale report from a different part of the season is unavailable, not re-dated',()=>{
  const ctx=context('seahawks');
  const parsed=parseInjuries(injuryHtml(ctx.team),{...ctx,now:'2026-12-01T20:00:00Z',roster:{players:[]}});
  assert.equal(parsed.available,false);assert.equal(parsed.records.length,0);
});
test('placeholder injury designation is not treated as a medical status',()=>{
  const ctx=context('broncos');
  for (const designation of ['','(-)','—','UNSPECIFIED','Not listed']) {
    const html=`${title(ctx.team)}<option value="/team/injury-report/week/REG-1" selected>1</option>${injuryTable(ctx.team.name,'Test Player','test-player',['LP','FP','FP',designation])}`;
    assert.equal(parseInjuries(html,{...ctx,roster:{players:[]}}).records.filter(row=>row.reportType==='Game Status').length,0);
  }
});
test('missing injury table and unknown status fail rather than falsely clearing injuries',()=>{
  const ctx=context('broncos');
  assert.throws(()=>parseInjuries(title(ctx.team),{...ctx,roster:{players:[]}}),/exactly one/);
  const html=`${title(ctx.team)}<option value="/team/injury-report/week/REG-1" selected>1</option>${injuryTable(ctx.team.name,'Test Player','test-player',['UNKNOWN','FP','FP','(-)'])}`;
  assert.throws(()=>parseInjuries(html,{...ctx,roster:{players:[]}}),/practice status/);
});
test('HTTP or malformed source failure rejects the entire candidate without mutating previous data',async()=>{
  const req=request('seahawks'),copy=JSON.stringify(req);
  await assert.rejects(collectTeam(req,{fetchImpl:async()=>({ok:false,status:503})}),/HTTP 503/);
  assert.equal(JSON.stringify(req),copy);
});
test('external source redirects are not followed',async()=>{
  let calls=0;
  await assert.rejects(collectTeam(request('broncos'),{fetchImpl:async()=>{calls++;return{status:302,headers:{get:()=> 'https://example.com/untrusted'}};}}),/redirect left/);
  assert.equal(calls,3);
});
test('legacy untagged seed is allowed only for Seattle, and wrong-team archives fail before fetch',async()=>{
  const req=request('broncos');req.previous.injuries={records:[]};
  let calls=0;await assert.rejects(collectTeam(req,{fetchImpl:()=>{calls++;}}),/another team's/);assert.equal(calls,0);
});
test('a partial first-seed roster with fewer than forty players is rejected',()=>{
  const ctx=context('seahawks'),rows=parseRoster(rosterHtml(ctx.team),ctx.team).slice(0,39);
  assert.throws(()=>reconcileRoster(null,rows,ctx),/Implausible roster count/);
});
test('January transactions use the new calendar year while the roster retains its NFL season',async()=>{
  const req=request('chiefs');req.now='2027-01-12T20:00:00Z';
  const team=teamFor('chiefs'),seen=[];
  const fetchImpl=async url=>{seen.push(url);return {ok:true,status:200,text:async()=> url.includes('/transactions/')
    ? transactionsHtml(team,'Signed QB New Player.',2027).replace('>September<','>January<').replace('>09/10<','>01/10<')
    : url.includes('players-roster') ? rosterHtml(team) : injuryHtml(team)};};
  const result=await collectTeam(req,{fetchImpl});
  assert(seen.includes(`https://${team.domain}/team/transactions/2027`));
  assert.equal(result.roster.season,2026);assert.equal(result.report.transactionYear,2027);
  assert.equal(result.transactions.records[0].timestamp,'2027-01-10T12:00:00Z');
});
test('future-dated official transactions fail rather than become published facts',()=>{
  const ctx=context('chiefs');
  const html=transactionsHtml(ctx.team).replace('>09/10<','>09/13<');
  assert.throws(()=>parseTransactions(html,{...ctx,roster:{players:[]}}),/dated in the future/);
});
test('an empty verified injury table clears current report keys while preserving the dated archive',()=>{
  const ctx=context('seahawks');
  const html=injuryHtml(ctx.team).replace(/<tbody>[\s\S]*?<\/tbody>/g,'<tbody></tbody>');
  const parsed=parseInjuries(html,{...ctx,roster:{players:[]}});
  assert.equal(parsed.available,true);assert.equal(parsed.period.week,1);
  const previous={asOf:'2026-09-10T12:00:00Z',records:[{date:'2026-09-10T12:00:00Z',playerId:'old',reportType:'Game Status',status:'Out'}]};
  const next=reconcileInjuries(previous,parsed,ctx);
  assert.deepEqual(next.currentReportKeys,[]);assert.deepEqual(next.records,previous.records);
  assert.equal(parseInjuries(html,{...ctx,nfl:null,roster:{players:[]}}).available,false);
});
test('an unconfirmed date-only kickoff cannot be used to infer injury dates',()=>{
  for (const flag of [{dateConfirmed:false},{date_confirmed:false},{date_tbd:true},{dateTbd:true}]) {
    const ctx=context('seahawks');Object.assign(ctx.nfl.schedule.gamesRegular[0],flag);
    const parsed=parseInjuries(injuryHtml(ctx.team),{...ctx,roster:{players:[]}});
    assert.equal(parsed.available,false);assert.deepEqual(parsed.records,[]);assert.match(parsed.reason,/kickoff date is unavailable/);
  }
});
test('an impossible calendar date is unavailable rather than normalized to another day',()=>{
  const ctx=context('seahawks');ctx.nfl.schedule.gamesRegular[0].date='2026-09-31';
  const parsed=parseInjuries(injuryHtml(ctx.team),{...ctx,roster:{players:[]}});
  assert.equal(parsed.available,false);assert.deepEqual(parsed.records,[]);assert.match(parsed.reason,/kickoff date is unavailable/);
});
test('unsupported injury phase and mismatched schedule year remain unavailable',()=>{
  const ctx=context('seahawks');
  for(const phase of ['PRE','POST','WC','DIV','CONF','SB']) {
    const parsed=parseInjuries(injuryHtml(ctx.team).replace('/REG-1','/'+phase+'-1'),{...ctx,roster:{players:[]}});
    assert.equal(parsed.available,false);assert.deepEqual(parsed.records,[]);
  }
  ctx.nfl.schedule.season=2025;
  assert.equal(parseInjuries(injuryHtml(ctx.team),{...ctx,roster:{players:[]}}).available,false);
});
test('a report unchanged from a previous NFL season is not re-dated to the new schedule',()=>{
  const ctx=context('seahawks');
  const first=parseInjuries(injuryHtml(ctx.team),{...ctx,roster:{players:[]}});
  const previous=reconcileInjuries(null,first,ctx);
  const future={...ctx,season:2027,now:'2027-09-12T20:00:00Z',nfl:{schedule:{season:2027,gamesRegular:[{id:2,week:1,date:'2027-09-13'}]}}};
  const parsed=parseInjuries(injuryHtml(ctx.team),{...future,roster:{players:[]},previousInjuries:previous});
  assert.equal(parsed.available,false);assert.match(parsed.reason,/another NFL season/);assert.deepEqual(parsed.records,[]);
  const retained=reconcileInjuries(previous,parsed,future);
  assert.equal(retained.sourceReportSeason,2026);assert.equal(retained.sourceReportFingerprint,previous.sourceReportFingerprint);assert.deepEqual(retained.currentReportKeys,[]);
});
