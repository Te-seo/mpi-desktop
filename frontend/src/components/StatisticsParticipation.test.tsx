import {act,render,screen,waitFor} from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import {beforeEach,expect,it,vi} from 'vitest'
import {StatisticsParticipation} from './StatisticsParticipation'
vi.mock('../lib/i18n',()=>({useLocale:()=>({locale:'ko'})}))
const invoke=vi.fn(),owner='11111111-1111-4111-8111-111111111111',other='22222222-2222-4222-8222-222222222222'
const state=(extra={})=>({available:true,preference:'inherit',legal_gate:'unresolved',consent_state:'unknown',revision:0,effective_inclusion:false,prompt_required:true,current_notice:{version:'synthetic-v1',content:'Synthetic notice'},...extra})
const result=(extra={})=>({user_id:owner,state:state(),pending:null,error:null,...extra})
beforeEach(()=>{invoke.mockReset();invoke.mockResolvedValue(result());Object.defineProperty(window,'api',{configurable:true,value:{invoke}})})
it('shows only the labelled switch without a first-use popup or inferred write; personal analysis remains mounted',async()=>{
 render(<><StatisticsParticipation ownerId={owner}/><div>Personal analysis workspace</div></>)
 const toggle=await screen.findByRole('switch',{name:'성능 통계 제공'});await waitFor(()=>expect(toggle).toBeEnabled())
 expect(toggle).toHaveAttribute('aria-checked','true');expect(toggle).toHaveTextContent('ON');expect(screen.queryByRole('dialog')).toBeNull()
 expect(screen.queryByText('Synthetic notice')).toBeNull();expect(screen.getAllByRole('switch')).toHaveLength(1)
 expect(invoke).toHaveBeenCalledTimes(1);expect(screen.getByText('Personal analysis workspace')).toBeVisible()
})
it('OFF saves exclude and ON saves inherit using keyboard; neither creates consent',async()=>{
 render(<StatisticsParticipation ownerId={owner}/>);const toggle=await screen.findByRole('switch');await waitFor(()=>expect(toggle).toBeEnabled())
 invoke.mockResolvedValueOnce(result({state:state({preference:'exclude',revision:1})}))
 toggle.focus();await userEvent.keyboard(' ')
 expect(invoke.mock.calls[1][1]).toMatchObject({user_id:owner,command:{action:'set_preference',preference:'exclude',expected_revision:0,notice_version:'synthetic-v1'}})
 await waitFor(()=>expect(toggle).toHaveAttribute('aria-checked','false'))
 invoke.mockResolvedValueOnce(result({state:state({revision:2,legal_gate:'prior_consent_required'})}))
 toggle.focus();await userEvent.keyboard('{Enter}')
 expect(invoke.mock.calls[2][1].command).toMatchObject({action:'set_preference',preference:'inherit',expected_revision:1})
 expect(invoke.mock.calls[2][1].command).not.toHaveProperty('consent');expect(toggle).toHaveAttribute('aria-checked','true')
})
it('restores the acknowledged preference after an offline result and retries only the same pending request through the toggle',async()=>{
 render(<StatisticsParticipation ownerId={owner}/>);const toggle=await screen.findByRole('switch');await waitFor(()=>expect(toggle).toBeEnabled())
 invoke.mockResolvedValueOnce(result({pending:{action:'set_preference',preference:'exclude',request_id:'pending',expected_revision:0,notice_version:'synthetic-v1'},error:'statistics-offline-pending'}))
 await userEvent.click(toggle);expect(await screen.findByRole('alert')).toBeVisible();expect(toggle).toHaveAttribute('aria-checked','true')
 invoke.mockResolvedValueOnce(result({state:state({preference:'exclude',revision:1})}));await userEvent.click(toggle)
 expect(invoke).toHaveBeenLastCalledWith('statistics-participation-set',{user_id:owner,retry:true})
 await waitFor(()=>expect(toggle).toHaveAttribute('aria-checked','false'));expect(screen.queryByRole('alert')).toBeNull()
})
it('keeps the stored OFF preference after remount or an owner response and ignores a late old-account save',async()=>{
 invoke.mockResolvedValueOnce(result({state:state({preference:'exclude',revision:3})}))
 const view=render(<StatisticsParticipation ownerId={owner}/>);const toggle=await screen.findByRole('switch');await waitFor(()=>expect(toggle).toBeEnabled());expect(toggle).toHaveAttribute('aria-checked','false')
 let resolve:(v:unknown)=>void=()=>{};invoke.mockImplementationOnce(()=>new Promise(r=>{resolve=r}));await userEvent.click(toggle)
 invoke.mockResolvedValueOnce(result({user_id:other,state:state({preference:'exclude'})}));view.rerender(<StatisticsParticipation ownerId={other}/>)
 await waitFor(()=>expect(screen.getByRole('switch')).toBeEnabled());expect(screen.getByRole('switch')).toHaveAttribute('aria-checked','false')
 await act(async()=>{resolve(result({state:state({preference:'inherit'})}))});expect(screen.getByRole('switch')).toHaveAttribute('aria-checked','false')
})
it('a rejected save restores OFF and keeps the same personal workspace',async()=>{
 invoke.mockResolvedValueOnce(result({state:state({preference:'exclude'})}));render(<><StatisticsParticipation ownerId={owner}/><div>Personal analysis workspace</div></>)
 const toggle=await screen.findByRole('switch');await waitFor(()=>expect(toggle).toBeEnabled());invoke.mockRejectedValueOnce(Error('network'))
 await userEvent.click(toggle);expect(await screen.findByRole('alert')).toBeVisible();expect(toggle).toHaveAttribute('aria-checked','false');expect(screen.getByText('Personal analysis workspace')).toBeVisible()
})
it('a conflict requires a fresh explicit versioned choice instead of retrying the stale revision',async()=>{
 render(<StatisticsParticipation ownerId={owner}/>);const toggle=await screen.findByRole('switch');await waitFor(()=>expect(toggle).toBeEnabled())
 invoke.mockResolvedValueOnce(result({state:state({revision:4}),pending:{action:'set_preference',preference:'exclude',request_id:'old',expected_revision:0,notice_version:'synthetic-v1'},error:'statistics-conflict'}))
 await userEvent.click(toggle);await screen.findByRole('alert');invoke.mockResolvedValueOnce(result({state:state({preference:'exclude',revision:5})}));await userEvent.click(toggle)
 expect(invoke.mock.calls[2][1]).toMatchObject({user_id:owner,command:{action:'set_preference',preference:'exclude',expected_revision:4}});expect(invoke.mock.calls[2][1]).not.toHaveProperty('retry')
})
