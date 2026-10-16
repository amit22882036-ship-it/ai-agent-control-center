import test from 'node:test'
import assert from 'node:assert/strict'
import { loadProjects, projectRequest, selectedProject } from '../src/projectCreation.mjs'
import { creationPayload } from '../src/autonomousCreation.mjs'
const projects = [{project_id:'one',name:'First',root_path:'C:/repos/First project'}, {project_id:'two',name:'Second',root_path:'C:/repos/Second'}]
const response = data => ({ok:true,json:async()=>data})

test('project loading uses authoritative API and allows an empty registry without a default', async()=>{
  let requested
  assert.deepEqual(await loadProjects(undefined,async url=>{requested=url;return response({projects})}),projects)
  assert.equal(requested,'http://127.0.0.1:8000/projects')
  assert.deepEqual(await loadProjects(undefined,async()=>response({projects:[]})),[])
  assert.equal(selectedProject(projects,''),null)
})
test('selection returns name and repository path, and stale selection never falls back',()=>{
  assert.equal(selectedProject(projects,'two'),projects[1])
  assert.equal(selectedProject(projects,'gone'),null)
  assert.equal(selectedProject([], 'one'),null)
})
test('load failures and malformed records fail closed with useful errors', async()=>{
  await assert.rejects(loadProjects(undefined,async()=>{throw Error('Offline')}),/Offline/)
  await assert.rejects(loadProjects(undefined,async()=>response({projects:[{project_id:'missing-fields'}]})),/unavailable/)
  await assert.rejects(loadProjects(undefined,async()=>({ok:false,json:async()=>({detail:'Storage unavailable'})})),/Storage unavailable/)
})
test('registration sends only explicit name and root path; duplicate errors remain actionable',async()=>{
  let request
  const signal = new AbortController().signal
  const item = await projectRequest('/projects',{signal,body:{name:'First',root_path:projects[0].root_path}},async(url,options)=>{request={url,options};return response(projects[0])})
  assert.equal(item,projects[0]);assert.equal(request.options.method,'POST');assert.equal(request.options.signal,signal)
  assert.deepEqual(JSON.parse(request.options.body),{name:'First',root_path:projects[0].root_path})
  await assert.rejects(projectRequest('/projects',{body:{}},async()=>({ok:false,json:async()=>({detail:'Project root duplicates or overlaps a registered Project'})})),/duplicates/)
})
test('root payload carries selected Project with autonomy and Mock remains simulated',()=>{
  for (const provider of ['mock','codex']) {
    const body = creationPayload(' Work ',provider,'read-only',true,null,'two')
    assert.equal(body.project_id,'two');assert.equal(body.autonomous,provider==='codex');assert.equal(body.task,'Work')
    assert.equal('root_path' in body,false)
  }
})
test('invalid explicit selections block payload construction and children retain inheritance',()=>{
  for(const id of [null,'','  ',3]) assert.throws(()=>creationPayload('Work','codex','read-only',true,null,id),/valid Project/)
  const child = creationPayload('Work','codex','read-only',true,'parent','other')
  assert.equal('project_id' in child,false);assert.equal(child.autonomous,false)
  assert.equal('project_id' in creationPayload('Work','mock','read-only',false),false)
})
