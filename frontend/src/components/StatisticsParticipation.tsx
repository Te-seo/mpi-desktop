import {useCallback,useEffect,useRef,useState} from 'react'
import {useLocale} from '../lib/i18n'

interface State {
 available:boolean;preference:'inherit'|'include'|'exclude';revision:number
 current_notice:{version:string;content:string}|null
}
interface Result {user_id:string;state:State|null;pending:unknown;error:string|null}
function pendingMatches(value:unknown,preference:string,state:State){
 if(!value||typeof value!=='object')return false
 const command=value as Record<string,unknown>
 return command.action==='set_preference'&&command.preference===preference&&command.expected_revision===state.revision&&command.notice_version===state.current_notice?.version
}
export function StatisticsParticipation({ownerId}:{ownerId:string}){
 const {locale}=useLocale(),ko=locale==='ko'
 const [result,setResult]=useState<Result|null>(null),[busy,setBusy]=useState(false),[error,setError]=useState(false)
 const generation=useRef(0),inFlight=useRef<string|null>(null)
 const load=useCallback(async()=>{
  if(inFlight.current===ownerId)return
  const version=++generation.current
  try{
   const next=await window.api.invoke<Result>('statistics-participation-get',{user_id:ownerId})
   if(next?.user_id!==ownerId)throw Error()
   if(version===generation.current){setResult(next);setError(Boolean(next.error||next.pending))}
  }catch{if(version===generation.current)setError(true)}
 },[ownerId])
 useEffect(()=>{
  setResult(null);setBusy(false);setError(false);void load()
  const refresh=()=>{void load()};window.addEventListener('online',refresh);window.addEventListener('focus',refresh)
  return()=>{++generation.current;window.removeEventListener('online',refresh);window.removeEventListener('focus',refresh)}
 },[load])
 async function toggle(){
  const s=result?.state
  if(inFlight.current===ownerId||!result||!s?.available||!s.current_notice)return
  const before=result,preference=s.preference==='exclude'?'inherit':'exclude',version=++generation.current
  inFlight.current=ownerId;setBusy(true);setError(false);setResult({...before,state:{...s,preference}})
  try{
   const retry=pendingMatches(before.pending,preference,s)
   const next=await window.api.invoke<Result>('statistics-participation-set',{user_id:ownerId,...(retry?{retry:true}:{command:{action:'set_preference',preference,request_id:crypto.randomUUID(),expected_revision:s.revision,notice_version:s.current_notice.version}})})
   if(next?.user_id!==ownerId)throw Error()
   if(version===generation.current){setResult(next);setError(Boolean(next.error||next.pending))}
  }catch{if(version===generation.current){setResult(before);setError(true)}}
  finally{if(inFlight.current===ownerId)inFlight.current=null;if(version===generation.current)setBusy(false)}
 }
 const checked=result?.user_id===ownerId&&result.state!==null&&result.state.preference!=='exclude'
 return <div className="rounded-xl border border-line bg-bg p-4 text-sm text-ink">
  <div className="flex items-center justify-between gap-4">
   <span id="statistics-settings-label">{ko?'성능 통계 제공':'Performance statistics'}</span>
   <button type="button" role="switch" aria-labelledby="statistics-settings-label" aria-checked={checked} aria-busy={busy}
    disabled={busy||result?.user_id!==ownerId||!result?.state?.available||!result.state.current_notice} onClick={()=>void toggle()}
    className={`inline-flex min-h-10 min-w-16 items-center justify-center rounded-xl border px-4 font-semibold focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-accent disabled:opacity-50 ${checked?'border-accent bg-accent text-bg':'border-line bg-bg text-muted'}`}>
    {checked?'ON':'OFF'}
   </button>
  </div>
  {error&&<p role="alert" className="mt-1 text-xs text-danger">{ko?'설정을 저장하지 못했습니다.':'Could not save this setting.'}</p>}
 </div>
}
