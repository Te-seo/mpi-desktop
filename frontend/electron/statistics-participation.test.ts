// @vitest-environment node
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import {afterEach,expect,it,vi} from 'vitest'
import {StatisticsParticipationClient,statisticsCommand} from './statistics-participation'
const a='11111111-1111-4111-8111-111111111111',b='22222222-2222-4222-8222-222222222222'
const dirs:string[]=[]
afterEach(()=>{for(const d of dirs.splice(0))fs.rmSync(d,{recursive:true,force:true})})
const state=(revision=0)=>({schema_version:1,purpose:'cross_user_statistics',available:true,preference:'inherit',consent_state:'unknown',legal_gate:'permitted_without_consent',revision,effective_inclusion:true,prompt_required:true,current_notice:{version:'test-v1',sha256:'a'.repeat(64),content:'Synthetic notice',privacy_url:'/legal/privacy'},personal_analysis_requires_participation:false})
const command=()=>({action:'set_preference',request_id:crypto.randomUUID(),expected_revision:0,notice_version:'test-v1',preference:'exclude'})
function file(){const d=fs.mkdtempSync(path.join(os.tmpdir(),'mpi-statistics-'));dirs.push(d);return path.join(d,'pending.json')}
it('persists offline stop intent, retries the same UUID after restart, never exposes credentials',async()=>{
 const name=file(),sent:unknown[]=[],body=command()
 const request=vi.fn(async(c?:unknown)=>{sent.push(c);if(c)throw Error('network');return{status:200,body:state()}})
 const first=new StatisticsParticipationClient(name,()=>a,request)
 await first.invoke({user_id:a});const offline=await first.invoke({user_id:a,command:body},true)
 expect(offline.pending).toEqual(body);expect(offline.state?.effective_inclusion).toBe(false);expect(offline.error).toBe('statistics-offline-pending')
 expect(fs.readFileSync(name,'utf8')).not.toMatch(/accessToken|password|Bearer/)
 const resume=vi.fn(async()=>({status:200,body:{...state(1),preference:'exclude',effective_inclusion:false}}))
 const second=new StatisticsParticipationClient(name,()=>a,resume)
 expect((await second.invoke({user_id:a,retry:true},true)).pending).toBe(null)
 expect(resume).toHaveBeenCalledWith(body)
})
it('pending choices are owner scoped and in-flight account switching discards acknowledgement',async()=>{
 let owner=a,finish:(v:{status:number;body:unknown})=>void=()=>{}
 const client=new StatisticsParticipationClient(file(),()=>owner,()=>new Promise(resolve=>{finish=resolve}))
 const pending=client.invoke({user_id:a});owner=b;finish({status:200,body:state()})
 await expect(pending).rejects.toThrow('account-changed')
 await expect(client.invoke({user_id:a})).rejects.toThrow('account-changed')
 await expect(client.invoke({user_id:b,retry:true},true)).rejects.toThrow('no-pending-statistics-request')
})
it('a conflict reads current revision without silently granting or replacing request IDs',async()=>{
 const request=vi.fn(async(body?:unknown)=>({status:body?409:200,body:state(4)})),client=new StatisticsParticipationClient(file(),()=>a,request),body=command()
 const result=await client.invoke({user_id:a,command:body},true)
 expect(result.error).toBe('statistics-conflict');expect(result.state?.revision).toBe(4);expect(result.state?.effective_inclusion).toBe(false)
 expect(result.pending).toEqual(body);expect(request).toHaveBeenCalledTimes(2)
})
it('does not accept renderer-supplied legal gates, consent flags or arbitrary routes',()=>{
 expect(()=>statisticsCommand({...command(),consent:true})).toThrow()
 expect(()=>statisticsCommand({...command(),legal_gate:'permitted_without_consent'})).toThrow()
 expect(()=>statisticsCommand({...command(),url:'https://evil.example'})).toThrow()
})
