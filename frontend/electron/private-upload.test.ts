// @vitest-environment node
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import {createHash} from 'node:crypto'
import {afterEach,expect,it} from 'vitest'
import {CloudSync} from './cloud-sync'
import {cloudProvider} from './cloud-provider'
const a='11111111-1111-4111-8111-111111111111',b='22222222-2222-4222-8222-222222222222'
const provider=cloudProvider({}),dirs:string[]=[],clients:CloudSync[]=[]
const secure={isEncryptionAvailable:()=>true,encryptString:(s:string)=>Buffer.from(s),decryptString:(s:Buffer)=>s.toString()}
const cap={contract_version:1,supported:true,enforcement_version:'private-measurement-v1',scope:'immutable_measurement_private_only'}
function seed(dir:string,owner=a){
 const folder=path.join(dir,provider.storageNamespace);fs.mkdirSync(folder,{recursive:true})
 fs.writeFileSync(path.join(folder,'cloud-auth.json'),JSON.stringify({encrypted:secure.encryptString(JSON.stringify({session:{userId:owner,email:'synthetic@example.invalid',accessToken:'a'.repeat(43),refreshToken:'r'.repeat(43),expiresAt:Date.now()+3600000},enabled:true})).toString('base64')}))
 const outbox=path.join(folder,'cloud-outbox');fs.mkdirSync(outbox,{recursive:true})
 const row={user_id:a,session_id:'collected-while-off',source:'desktop',metadata:{},summary:{},time_series:[],sample_count:0,duration_sec:0,name:null}
 const file=path.join(outbox,createHash('sha256').update(`${a}:${row.session_id}:desktop`).digest('hex')+'.json');if(!fs.existsSync(file))fs.writeFileSync(file,JSON.stringify(row))
 return file
}
function directory(){const dir=fs.mkdtempSync(path.join(os.tmpdir(),'mpi-private-queue-'));dirs.push(dir);return dir}
afterEach(()=>{for(const c of clients.splice(0))c.stop();for(const d of dirs.splice(0))fs.rmSync(d,{recursive:true,force:true})})
it('old queued rows require verified capability, remain private across offline restart/reparticipation and clear only on exact acknowledgement',async()=>{
 const dir=directory(),file=seed(dir),sent:unknown[]=[];let supported=false,offline=false,ack=false
 const request:ConstructorParameters<typeof CloudSync>[3]=async(url,_headers,body)=>{
  if(url.endsWith('/api/privacy/statistics-participation'))return{status:200,body:{account_id:a,effective_inclusion:true,capabilities:{private_only_upload:{...cap,supported}}}}
  if(url.endsWith('/api/desktop/sessions')){sent.push(body);if(offline)throw Error('network');return{status:200,body:ack?{ok:true,session_id:'collected-while-off',statistics_visibility:'private_only',statistics_enforcement_version:'private-measurement-v1'}:{ok:true,session_id:'collected-while-off'}}}
  return{status:200,body:{}}
 }
 let sync=new CloudSync(dir,secure,undefined,request);clients.push(sync);await sync.retry()
 expect(sent).toHaveLength(0);expect(sync.status().pending).toBe(1);expect(fs.existsSync(file)).toBe(true)
 supported=true;offline=true;await sync.retry();expect(sync.status().pending).toBe(1);sync.stop()
 sync=new CloudSync(dir,secure,undefined,request);clients.push(sync);offline=false;await sync.retry()
 expect(sync.status().pending).toBe(1);expect(fs.existsSync(file)).toBe(true)
 ack=true;await sync.retry();expect(sync.status().pending).toBe(0);expect(fs.existsSync(file)).toBe(false)
 for(const body of sent)expect(body).toMatchObject({user_id:a,session_id:'collected-while-off',statistics_visibility:'private_only'})
})
it('another signed-in account never drains the original owner private queue',async()=>{
 const dir=directory(),file=seed(dir);seed(dir,b);let calls=0
 const sync=new CloudSync(dir,secure,undefined,async()=>{calls++;return{status:200,body:{capabilities:{private_only_upload:cap}}}});clients.push(sync)
 await sync.retry();expect(calls).toBe(0);expect(sync.status().otherAccountPending).toBe(1);expect(fs.existsSync(file)).toBe(true)
})
