#!/usr/bin/env python3
"""Optional UI regression test: Firefox + Python websockets (not app dependencies).

Runs only against disposable loopback servers/data. Screenshots stay in the
printed temporary directory; browser profiles and databases are cleaned up.
"""
from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.request

try:
    import websockets
except ImportError:
    raise SystemExit("UI tests require the optional Python websockets package.")

ROOT = Path(__file__).resolve().parents[1]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def main() -> None:
    if not shutil.which("firefox"):
        raise SystemExit("UI tests require Firefox on PATH.")
    artifacts = Path(tempfile.mkdtemp(prefix="networkmap-ui-screenshots-"))
    print(f"Screenshots: {artifacts}", flush=True)
    with tempfile.TemporaryDirectory(prefix="networkmap-ui-test-") as temporary:
        folder = Path(temporary)
        server_port, browser_port = free_port(), free_port()
        base = f"http://127.0.0.1:{server_port}"
        processes = []
        ws = None
        try:
            processes.append(subprocess.Popen([
                sys.executable, "server.py", "--host", "127.0.0.1", "--port", str(server_port),
                "--data-dir", str(folder / "data")
            ], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            for _ in range(100):
                try:
                    with opener.open(base + "/api/health", timeout=1) as response:
                        assert response.status == 200
                    break
                except OSError:
                    await asyncio.sleep(.05)
            else:
                raise AssertionError("Temporary server did not start")
            profile = folder / "firefox"
            profile.mkdir()
            processes.append(subprocess.Popen([
                "firefox", "--headless", "--no-remote", "--profile", str(profile),
                "--remote-debugging-port", str(browser_port), "about:blank"
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            for _ in range(150):
                try:
                    ws = await websockets.connect(f"ws://127.0.0.1:{browser_port}/session", max_size=2**24)
                    break
                except OSError:
                    await asyncio.sleep(.1)
            assert ws is not None, "Temporary Firefox did not start"
            sequence = 0

            async def command(method, params):
                nonlocal sequence
                sequence += 1
                await ws.send(json.dumps({"id": sequence, "method": method, "params": params}))
                while True:
                    response = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
                    if response.get("id") != sequence:
                        continue
                    assert response.get("type") != "error", response
                    return response["result"]

            await command("session.new", {"capabilities": {}})
            context = (await command("browsingContext.getTree", {}))["contexts"][0]["context"]
            await command("script.addPreloadScript", {"functionDeclaration": """() => {
                window.testErrors = [];
                addEventListener('error', e => testErrors.push(e.message));
                addEventListener('unhandledrejection', e => testErrors.push(String(e.reason)));
            }"""})

            async def js(body):
                expression = """(async () => {
                    const get=id=>document.getElementById(id);
                    const check=(value,message)=>{if(!value)throw new Error(message);};
                    const wait=async predicate=>{for(let i=0;i<150;i++){if(predicate())return;await new Promise(r=>setTimeout(r,30));}throw new Error('UI did not settle');};
                    const state=()=>fetch('/api/state').then(r=>r.json());
                    const result=await (async()=>{ BODY })();
                    return JSON.stringify(result ?? null);
                })()""".replace("BODY", body)
                result = await command("script.evaluate", {"expression": expression, "target": {"context": context}, "awaitPromise": True})
                assert result["type"] == "success", result
                return json.loads(result["result"]["value"])

            async def screenshot(name):
                await js("document.querySelectorAll('.toast button').forEach(button=>button.click());")
                await asyncio.sleep(.3)
                result = await command("browsingContext.captureScreenshot", {"context": context})
                (artifacts / name).write_bytes(base64.b64decode(result["data"]))

            async def viewport(width, height):
                await command("browsingContext.setViewport", {"context": context, "viewport": {"width": width, "height": height}})
                await asyncio.sleep(.4)

            await viewport(1440, 1000)
            await command("browsingContext.navigate", {"context": context, "url": base, "wait": "complete"})
            await js("""
                await wait(()=>document.querySelectorAll('.node-device-image').length===6);
                check(!document.querySelector('.node-badge,.node-status'),'Healthy nodes have a badge/dot');
                check(get('topologyEditButton').getAttribute('aria-pressed')==='false','Map starts unlocked');
                document.documentElement.dataset.theme='dark';
            """)
            await screenshot("topology-dark.png")
            await js("document.documentElement.dataset.theme='light';localStorage.setItem('networkmap_theme','light');")
            await screenshot("topology-light.png")

            await js("""
                get('topologyEditButton').click(); get('addAreaButton').click();
                get('areaLabel').value='Office <VLAN> zone';get('areaVlan').value='20';
                get('areaX').value='150.5';get('areaY').value='-120.5';get('areaWidth').value='380';get('areaHeight').value='220';
                get('areaSubmit').click();await wait(()=>!get('areaDialog').open);
                check(document.querySelectorAll('.map-area').length===1,'Area not rendered');
                get('addAreaButton').click();get('areaLabel').value='Guest';get('areaShape').value='ellipse';get('areaVlan').value='30';
                get('areaX').value='600';get('areaY').value='0';get('areaWidth').value='300';get('areaEqualSides').click();
                get('areaColor').value='violet';get('areaSubmit').click();await wait(()=>!get('areaDialog').open);
                const ellipse=document.querySelector('.map-area ellipse');
                check(ellipse && ellipse.getAttribute('rx')===ellipse.getAttribute('ry'),'Circle geometry incorrect');
                document.querySelector('.link-group').dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}));
                check(get('speedPresetTableBody').children.length===13,'Missing speed table');
                get('linkSpeedPreset').value='2500';get('linkSpeedPreset').dispatchEvent(new Event('change'));get('linkDuplex').value='full';
                get('linkSubmit').click();await wait(()=>!get('linkDialog').open);
                document.querySelector('.node[data-node-id="demo-gateway"]').dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}));
                check(get('inspectorConnections').textContent.includes('2.5 Gbps') && get('inspectorConnections').textContent.includes('Full duplex'),'Missing inspector metrics');
                for(const value of ['0','150.5','']) {
                    document.querySelector('.link-group').dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}));
                    get('linkSpeedPreset').value=value===''?'':'custom';get('linkSpeedPreset').dispatchEvent(new Event('change'));get('linkSpeed').value=value;
                    get('linkSubmit').click();await wait(()=>!get('linkDialog').open);
                    document.querySelector('.link-group').dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}));
                    check(get('linkSpeedPreset').value===(value===''?'':'custom') && get('linkSpeed').value===value,'Custom speed round trip: '+value);
                    get('linkDialog').close();
                }
                document.querySelector('.link-group').dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}));
                get('linkSpeedPreset').value='10000';get('linkSpeedPreset').dispatchEvent(new Event('change'));get('linkSubmit').click();await wait(()=>!get('linkDialog').open);
            """)
            await screenshot("inspector-areas.png")
            await js("get('closeInspector').click();")
            await asyncio.sleep(.4)
            nodes_before = await js("return (await state()).nodes;")

            for resize in (False, True):
                geometry = await js("""
                    const area=(await state()).areas.find(a=>a.label.startsWith('Office'));
                    const r=document.querySelector(`[data-area-id="${area.id}"] .area-shape`).getBoundingClientRect();
                    return {area,rect:{left:r.left,top:r.top,right:r.right,bottom:r.bottom}};
                """)
                bounds = geometry["rect"]
                x = round(bounds["right"] if resize else bounds["left"] + 30)
                y = round(bounds["bottom"] if resize else bounds["top"] + 45)
                await command("input.performActions", {"context": context, "actions": [{"type": "pointer", "id": "areaMouse", "parameters": {"pointerType": "mouse"}, "actions": [
                    {"type": "pointerMove", "x": x, "y": y, "duration": 0, "origin": "viewport"},
                    {"type": "pointerDown", "button": 0},
                    {"type": "pointerMove", "x": x + 45, "y": y + 25, "duration": 300, "origin": "viewport"},
                    {"type": "pointerUp", "button": 0}
                ]}]})
                await asyncio.sleep(.4)
                after = await js("return await state();")
                changed = next(a for a in after["areas"] if a["id"] == geometry["area"]["id"])
                key = "width" if resize else "x"
                assert changed[key] > geometry["area"][key], ("Gesture did not persist", geometry, changed)
                assert after["nodes"] == nodes_before, "Area gesture moved devices"

            await js("""
                get('topologyEditButton').click();
                check(!document.querySelector('.map-area[tabindex]') && get('addAreaButton').hidden,'Areas editable while locked');
                document.querySelector('[data-view=configuration]').click();document.querySelector('[data-config-tab=areas]').click();
                check(get('areaTableBody').children.length===2,'Areas inventory missing');
                document.querySelector('[data-area-action=locate]').click();
                check(get('topologyEditButton').getAttribute('aria-pressed')==='false','Show area unlocks map');
                const exported=await fetch('/api/export').then(r=>r.json());
                check(exported.areas.length===2 && exported.links[0].bandwidth_mbps===10000 && exported.links[0].duplex==='full','Export loses new fields');
                for(const [id,status] of [['demo-server','offline'],['demo-gateway','degraded'],['demo-internet','unknown']]) {
                    const current=await state();
                    const response=await fetch('/api/nodes/'+id,{method:'PATCH',headers:{'Content-Type':'application/json','If-Match':String(current.revision)},body:JSON.stringify({status})});
                    check(response.ok,'Status fixture update failed');
                }
                await wait(()=>document.querySelectorAll('.node-status').length===2);
                check(document.querySelector('.node-status.offline') && document.querySelector('.node-status.degraded') && !document.querySelector('.node-status.online,.node-status.unknown'),'Error-only indicators incorrect');
                get('fitMap').click();
            """)
            await screenshot("vlan-areas.png")
            print("Area creation, real drag/resize, quiet icons, speed presets, custom speeds, duplex, export and lock checks passed.", flush=True)

            await js("get('topbarHideButton').click();get('sidebarCollapseButton').click();")
            await command("browsingContext.reload", {"context": context, "wait": "complete"})
            await js("""
                await wait(()=>document.querySelectorAll('.map-area').length===2);
                check(['topbar','overviewHeading','networkSummary'].every(id=>get(id).hidden),'Hidden overview not persisted');
                check(get('appShell').classList.contains('sidebar-collapsed'),'Sidebar setting not persisted');
            """)
            for width, height in [(1100,820),(931,700),(800,700),(400,800),(320,640),(800,400)]:
                await viewport(width, height)
                result = await js("""
                    if(innerWidth>=931 && get('appShell').classList.contains('sidebar-collapsed'))get('sidebarCollapseButton').click();
                    document.querySelector('.node[data-node-id="demo-switch"]').dispatchEvent(new MouseEvent('click',{bubbles:true}));
                    get('topologyEditButton').click();await new Promise(r=>setTimeout(r,400));
                    const panel=document.querySelector('.map-panel').getBoundingClientRect();
                    const fits=[...document.querySelector('.map-header-actions').children].every(e=>{const r=e.getBoundingClientRect();return e.hidden || (r.left>=panel.left && r.right<=panel.right);});
                    const a=get('topbarShowButton').getBoundingClientRect(),b=get('closeInspector').getBoundingClientRect();
                    const overlap=a.left<b.right && a.right>b.left && a.top<b.bottom && a.bottom>b.top;
                    get('topologyEditButton').click();get('closeInspector').click();get('topbarShowButton').click();
                    const restored=!get('overviewHeading').hidden&&!get('networkSummary').hidden;
                    get('topologyDataMenu').open=true;
                    const menu=document.querySelector('.topology-data-popover').getBoundingClientRect();
                    const map=document.querySelector('.map-panel').getBoundingClientRect();
                    get('topologyDataMenu').open=false;get('topbarHideButton').click();
                    return {width:innerWidth,scrollWidth:document.documentElement.scrollWidth,fits,overlap,restored,menuFits:menu.left>=map.left&&menu.right<=map.right};
                """)
                assert result["scrollWidth"] <= width and result["fits"] and result["restored"] and result["menuFits"] and not result["overlap"], result
                await screenshot(f"map-{width}x{height}.png")
            await js("""
                const before=await state();
                document.querySelector('[data-view=configuration]').click();document.querySelector('[data-config-tab=areas]').click();
                document.querySelector('[data-area-action=delete]').click();get('confirmButton').click();
                await wait(()=>get('areaTableBody').children.length===1);
                const after=await state();
                check(JSON.stringify(after.nodes)===JSON.stringify(before.nodes) && JSON.stringify(after.links)===JSON.stringify(before.links),'Removing area deletes equipment');
                const large={...after,nodes:[],links:[],areas:[{...after.areas[0],x:50000,y:10000,width:30000,height:20000}]};
                const response=await fetch('/api/state',{method:'PUT',headers:{'Content-Type':'application/json','If-Match':String(after.revision)},body:JSON.stringify(large)});
                check(response.ok,'Areas-only fixture failed');
                await wait(()=>get('statDevices').textContent==='0');
                document.querySelector('[data-view=overview]').click();await new Promise(r=>setTimeout(r,250));
                check(get('mapEmpty').classList.contains('hidden') && document.querySelector('.map-area'),'Areas-only workspace hidden');
                const transform=()=>get('viewport').transform.baseVal.consolidate().matrix;
                const scale=transform().a;check(scale<.25,'Large area not fitted');get('zoomOut').click();
                check(transform().a<scale,'Zoom out jumps inward');
            """)
            assert await js("return testErrors;") == []
            await js("await navigator.serviceWorker.ready;")
            processes[0].terminate()
            processes[0].wait(timeout=8)
            await command("browsingContext.reload", {"context": context, "wait": "complete"})
            offline = await js("""
                const cache=await caches.open('networkmap-shell-v10');
                const keys=await cache.keys();
                const images=keys.filter(r=>r.url.includes('/static/devices/'));
                for(const request of images){const img=new Image();img.src=request.url;await img.decode();}
                check(!!get('topology') && typeof createNetworkMapAreas==='function','Offline shell/module missing');
                check(images.length===15,'Offline images missing');
                return {images:images.length,errors:testErrors};
            """)
            assert offline["errors"] == [], offline
            print("Responsive/hide controls and offline shell + all 15 device images passed.", flush=True)
            await command("session.end", {})
        finally:
            if ws is not None:
                await ws.close()
            for process in reversed(processes):
                if process.poll() is not None:
                    continue
                process.terminate()
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


if __name__ == "__main__":
    asyncio.run(main())
