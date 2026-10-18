// Opt-in physical UART1/camera verification in the actual Electron renderer.
const fs = require("node:fs");
const path = require("node:path");
const { _electron } = require("playwright");
const root = path.resolve(__dirname, "..");
const packaged = process.argv.includes("--packaged");
const host = process.env.LF2S_ESP32_HOST;
if (!host) throw new Error("Set LF2S_ESP32_HOST to the paired board's current IPv4");
const suffix = packaged ? "package" : "dev";
const checks = [], errors = [];
const report = {passed:false, scope:"Real Electron window + ESP32 camera + STM32 persistent UART1 read-only telemetry; no flight", checks};
function check(name, value) { checks.push({name,passed:!!value}); if(!value)throw new Error(name); }
const delay = ms => new Promise(resolve=>setTimeout(resolve,ms));
async function until(test, message, timeout=15000) {
  const end=Date.now()+timeout;
  while(Date.now()<end) { const result=await test(); if(result) return result; await delay(100); }
  throw new Error(message);
}
async function main() {
  const env={...process.env, LF2S_STATION_USER_DATA:path.join(root,`.work/qa-uart1-desktop-${suffix}`)};
  for(const key of ["ELECTRON_RUN_AS_NODE","PYTHONHOME","PYTHONPATH","VIRTUAL_ENV","NODE_OPTIONS","NODE_PATH"])delete env[key];
  let instance;
  try {
    const distribution=packaged ? path.join(root,"release",JSON.parse(fs.readFileSync(path.join(root,"release/latest.json"))).directory) : root;
    if(packaged)env.PATH=path.join(process.env.SystemRoot,"System32");
    instance=await _electron.launch({executablePath:packaged ? path.join(distribution,"LF2S-Station.exe") : require("electron"),args:packaged?[]:[root],env,timeout:20000});
    const page=await instance.firstWindow();
    page.on("pageerror",error=>errors.push(error.message));
    await until(async()=>(await page.evaluate(()=>window.lf2s.state())).service_ready,"service_not_ready");
    await page.getByTestId("workspace-tools").click();
    await page.getByRole("button",{name:"ESP32 实物台架",exact:false}).click();
    await page.getByLabel("ESP32 IPv4 地址",{exact:true}).fill(host);
    await page.getByRole("button",{name:"连接实物台架",exact:true}).click();
    await until(async()=>(await page.evaluate(()=>window.lf2s.state())).bench?.phase==="online","camera_not_online");
    await page.getByRole("button",{name:"连接 UART1 遥测",exact:true}).click();
    await until(async()=>{
      const s=await page.evaluate(()=>window.lf2s.state());
      return s.mode==="uart_readonly"&&s.link.phase==="online"&&s.sensors.attitude.valid;
    },"real_attitude_not_online");
    const state=await page.evaluate(()=>window.lf2s.state());
    check("real_source_and_readonly",!state.simulation&&state.read_only&&state.hardware_control_allowed===false&&state.sensors.attitude.source==="uart1");
    check("real_boot_session_identity",!!state.identity.boot_id&&!!state.identity.session_id&&typeof state.identity.session_id==="string");
    await page.getByRole("button",{name:"读取飞控配置",exact:true}).click();
    await until(async()=>!!(await page.evaluate(()=>window.lf2s.state())).profile,"profile_not_received");
    await until(async()=>(await page.getByTestId("uart1-values").innerText()).includes("°"),"attitude_not_visible_in_tools");
    check("attitude_visible_in_tools",true);
    await page.screenshot({path:path.join(root,`evidence/uart1-${suffix}-tools.png`),fullPage:true});
    await page.getByRole("button",{name:"返回工作台",exact:false}).click();
    await until(async()=>(await page.getByTestId("workbench-uart1-attitude").innerText()).includes("°"),"workbench_attitude_missing");
    await until(async()=>await page.getByTestId("real-image").isVisible(),"real_camera_image_missing");
    await delay(4500);
    const later=await until(async()=>{
      const recovered=await page.evaluate(()=>window.lf2s.state());
      return recovered.link.phase==="online" && Number(recovered.identity.sample_id)>Number(state.identity.sample_id) && recovered.sensors.attitude.valid ? recovered : null;
    },"attitude_not_resuming_after_transport_gap",8000);
    report.later=Object.fromEntries(["link","identity","sensors","counters"].map(key=>[key,later[key]]));
    check("telemetry_survives_gateway_idle_window",later.link.phase==="online"&&Number(later.identity.sample_id)>Number(state.identity.sample_id));
    check("report_records_fresh_recovered_attitude",later.sensors.attitude.valid);
    check("camera_and_telemetry_together",await page.getByTestId("real-image").isVisible());
    const geometry=await page.getByTestId("workbench-uart1-attitude").evaluate(element=>{
      const value=element.getBoundingClientRect(), card=element.closest('[data-testid="workbench-uart1"]').getBoundingClientRect();
      return value.top>=card.top && value.bottom<=card.bottom && value.left>=card.left && value.right<=card.right;
    });
    check("attitude_fits_visible_card_without_internal_scrolling",geometry);
    check("connection_strip_reports_real_uart1",(await page.getByTestId("workbench-connection-strip").innerText()).includes("UART1只读"));
    await until(async()=>(await page.getByTestId("workbench-uart1-attitude").innerText()).includes("°"),"fresh_attitude_not_rendered_for_capture",8000);
    await page.screenshot({path:path.join(root,`evidence/uart1-${suffix}-workbench.png`),fullPage:true});
    report.identity=later.identity;
    report.attitude=later.sensors.attitude;
    report.application_version=later.application_version;
    await page.evaluate(()=>window.lf2s.gatewayDisconnect());
    await until(async()=>!(await page.evaluate(()=>window.lf2s.state())).gateway.active,"disconnect_failed");
    check("disconnect_clears_current_identity",!(await page.evaluate(()=>window.lf2s.state())).identity.boot_id);
    await page.evaluate(()=>window.lf2s.benchDisconnect());
    check("no_renderer_errors",errors.length===0);
    report.passed=true;
  } catch(error) {report.error=String(error.stack||error); throw error;}
  finally {
    if(instance)await instance.close();
    report.errors=errors;
    fs.writeFileSync(path.join(root,`evidence/uart1-${suffix}-live.json`),JSON.stringify(report,null,2)+"\n");
    console.log(JSON.stringify(report));
  }
}
main().catch(error=>{console.error(error);process.exitCode=1;});
