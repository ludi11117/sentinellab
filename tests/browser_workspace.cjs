const {chromium} = require(process.env.PLAYWRIGHT_MODULE || '../tooling/node_modules/playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname,'..');
const host = process.env.TEST_HOST || 'http://127.0.0.1:8000';
const artifact = name => path.join(root,'artifacts',name);

async function run(mode = 'smoke') {
  const browser = await chromium.launch({headless:true,...(process.env.CHROME_EXECUTABLE?{executablePath:process.env.CHROME_EXECUTABLE}:{})});
  try {
    const page = await browser.newPage({viewport:{width:1440,height:1000},acceptDownloads:true});
    page.setDefaultTimeout(15000);
    async function shot(name) {await page.evaluate(()=>scrollTo(0,0));if(await page.locator('#notice-dismiss').isVisible())await page.locator('#notice-dismiss').click();await page.evaluate(()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r))));await page.screenshot({path:artifact(name),fullPage:true});}
    const errors=[]; page.on('pageerror',e=>errors.push(e.message));
    await page.goto(host+'/delivery.html');
    await page.waitForURL('**/#setup');
    await page.locator('.template').first().waitFor();
    assert.equal(await page.locator('.template').count(),3);
    await page.locator('[data-action=template][data-id=ops]').click();
    await page.locator('main').filter({hasText:'16 道题'}).waitFor();
    await shot('workspace-setup.png');
    if (mode==='smoke') {
      for (const [old,view] of Object.entries({releases:'compare',evidence:'compare',review:'review',learn:'guide'})) {
        await page.goto(host+'/'+old+'.html'); await page.waitForURL('**/#'+view);
        await page.waitForFunction(()=>document.querySelector('#app').getAttribute('aria-busy')==='false');
      }
      await page.setViewportSize({width:390,height:844});
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
      await page.goto(host+'/#setup'); await page.locator('.template').first().waitFor();
      await shot('workspace-mobile.png');
      assert.deepEqual(errors,[]); console.log('PASS: one workspace, three templates, all legacy redirects, mobile overflow, no page errors.'); return;
    }
    if (mode==='quick') { await page.locator('[data-action=template][data-id=demo]').click(); await page.locator('main').filter({hasText:'36 道题'}).waitFor(); }
    await page.locator('[data-action=import]').click(); await page.waitForURL('**/#run');
    await page.locator('[data-action=run-slot][data-slot=baseline]:enabled').waitFor();
    const before=await page.request.get(host+'/api/public/datasets').then(r=>r.json());
    await page.locator('[data-action=run-slot][data-slot=baseline]').click();
    await page.locator('[data-action=run-slot][data-slot=candidate]:enabled').waitFor();
    let accepted;
    if (mode==='quick') {
      accepted=new Promise(resolve=>{
        page.route('**/api/public/jobs',async route=>{
          if(route.request().method()!=='POST') return route.continue();
          const response=await route.fetch(); resolve();
          await new Promise(r=>setTimeout(r,2000));
          try { await route.fulfill({response}); } catch { /* Reload deliberately aborts the response. */ }
        });
      });
    }
    await page.locator('[data-action=run-slot][data-slot=candidate]').click();
    // Reload while jobs execute: no new dataset or job is submitted by restoring a view.
    if (accepted) await accepted;
    else await page.waitForFunction(()=>JSON.parse(sessionStorage.getItem('sentinel-workspace-local'))?.candidateJobId);
    await page.reload();
    await page.waitForFunction(()=>document.querySelector('#app').getAttribute('aria-busy')==='false');
    const after=await page.request.get(host+'/api/public/datasets').then(r=>r.json());
    assert.equal(before.length,after.length);
    await page.locator('[data-action=go-compare]:enabled').waitFor({timeout:180000});
    await page.locator('[data-action=go-compare]').click(); await page.waitForURL('**/#compare');
    await page.locator('[data-action=compare]:enabled').waitFor();
    const selected=await page.locator('#candidate-select').inputValue();
    await page.reload(); await page.locator('[data-action=compare]:enabled').waitFor();
    assert.equal(await page.locator('#candidate-select').inputValue(),selected);
    await page.locator('[data-action=compare]').click(); await page.locator('.verdict').waitFor();
    await page.locator('#case-filter').selectOption('all');
    const cases=mode==='quick'?36:16;
    await page.locator('[data-action=case-detail]').first().waitFor();
    assert.equal(await page.locator('[data-action=case-detail]').count(),cases);
    // A different dataset never appears among comparison candidates.
    const session=await page.evaluate(()=>JSON.parse(sessionStorage.getItem('sentinel-workspace-local')));
    const experiments=await page.request.get(host+'/api/public/experiments').then(r=>r.json());
    const base=experiments.find(e=>e.id===session.baselineId);
    const ids=await page.locator('#candidate-select option').evaluateAll(nodes=>nodes.map(n=>n.value).filter(Boolean));
    assert(ids.every(id=>experiments.some(e=>e.id===id && e.dataset_id===session.datasetId && e.evaluator_version===base.evaluator_version)));
    await page.locator('[data-action=case-detail]').first().click();
    await page.locator('#detail-dialog[open]').waitFor();
    assert.match(await page.locator('#detail-body').innerText(),/这道题怎样评分/);
    await page.locator('#detail-dialog [data-close]').click();
    const downloaded=page.waitForEvent('download'); await page.locator('[data-action=download-report]').click();
    const file=await downloaded; await file.saveAs(artifact('workspace-'+mode+'-delivery.json'));
    await page.locator('[data-action=start-review]:enabled').waitFor();
    await shot('workspace-'+mode+'-compare.png');
    await page.locator('[data-action=start-review]').click(); await page.waitForURL('**/#review');
    await page.locator('#vote-form').waitFor();
    assert.equal(await page.locator('main .pair-answer').count(),2);
    assert.equal(await page.locator('[data-action=sign]').isDisabled(),true);
    const fullSession=await page.evaluate(()=>JSON.parse(sessionStorage.getItem('sentinel-workspace-local')));
    const review=await page.request.get(host+'/api/public/reviews/'+fullSession.roundId+'/tasks').then(r=>r.json());
    assert(!JSON.stringify(review).includes('trace_id'));
    assert(!JSON.stringify(review).includes('llm-grounded-v'));
    await shot('workspace-'+mode+'-review.png');
    const report={measured_at:new Date().toISOString(),provenance:'developer_agent_browser_dogfood',mode,session:fullSession,
      experiments:await Promise.all([fullSession.baselineId,fullSession.candidateId].map(id=>page.request.get(host+'/api/public/experiments/'+id).then(r=>r.json()))),
      gate:(await page.request.get(host+'/api/public/gates').then(r=>r.json())).find(g=>g.id===fullSession.gateId),review,
      browser_checks:{refresh_preserved_dataset:true,refresh_preserved_pair:true,compatible_candidates_only:true,trace_drilldown:true,download:true,no_script_errors:true},
      human_review:'pending_real_human',limitations:'Developer self-test on already seen regression cases, not independent holdout or customer acceptance.'};
    fs.writeFileSync(artifact('dogfood-'+mode+'.json'),JSON.stringify(report,null,2));
    if (mode==='quick') {
      // A denied tool plan must be visible as a blocked delivery, not just a high aggregate score.
      await page.goto(host+'/#run'); await page.locator('#cand-revision').selectOption('unsafe-v3');
      await page.locator('[data-action=run-slot][data-slot=candidate]:enabled').waitFor();
      await page.locator('[data-action=run-slot][data-slot=candidate]').click();
      await page.locator('[data-action=go-compare]:enabled').waitFor({timeout:30000});
      await page.locator('[data-action=go-compare]').click(); await page.locator('[data-action=compare]:enabled').waitFor();
      await page.locator('[data-action=compare]').click(); await page.locator('.verdict.block').waitFor();
      assert.match(await page.locator('main').innerText(),/暂缓交付/);
      report.browser_checks.denied_tool_blocks_release=true;
      report.browser_checks.interrupted_post_response_recovered=true;
      fs.writeFileSync(artifact('dogfood-quick.json'),JSON.stringify(report,null,2));
      await page.locator('#case-filter').selectOption('failures');
      await shot('workspace-blocked.png');
    }
    await page.setViewportSize({width:390,height:844});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    await shot('workspace-'+mode+'-mobile.png');
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({result:'PASS',mode,summaries:report.experiments.map(e=>e.summary),decision:report.gate.decision,review_round:fullSession.roundId,artifact:artifact('dogfood-'+mode+'.json')}));
  } finally { await browser.close(); }
}
module.exports=run;
if(require.main===module) run(process.argv[2] || 'smoke').catch(error=>{console.error(error);process.exitCode=1;});
