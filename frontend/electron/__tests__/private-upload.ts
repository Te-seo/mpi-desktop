import {CloudSync} from '../cloud-sync'
/** Synthetic server support for existing queue/auth fixtures. Dedicated privacy
 * tests use the real CloudSync transport without this adapter. */
export class FixtureCloudSync extends CloudSync {
 constructor(...args:ConstructorParameters<typeof CloudSync>){
  const [directory,secure,onStatus,request,now,oauth,provider]=args
  let currentUser:()=>string|null=()=>null
  const transport:ConstructorParameters<typeof CloudSync>[3]=request && (async(url,headers,body,method)=>{
   if(url.endsWith('/api/privacy/statistics-participation'))return{status:200,body:{account_id:currentUser(),capabilities:{private_only_upload:{contract_version:1,supported:true,enforcement_version:'private-measurement-v1',scope:'immutable_measurement_private_only'}}}}
   const response=await request(url,headers,body,method)
   if(url.endsWith('/api/desktop/sessions')&&response.status>=200&&response.status<300&&response.body===null)return{...response,body:{ok:true,session_id:(body as {session_id:string}).session_id,statistics_visibility:'private_only',statistics_enforcement_version:'private-measurement-v1'}}
   return response
  })
  super(directory,secure,onStatus,transport,now,oauth,provider)
  currentUser=()=>this.status().userId
 }
}
