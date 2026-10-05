const csrf = document.querySelector('meta[name="csrf-token"]').content;
const $ = id => document.getElementById(id);
const labels = {starting:'准备中',active:'可操作',completed:'已完成',verifying:'旧校验中',passed:'旧任务已通过',failed:'旧任务未通过',error:'连接异常',expired:'已过期',cancelled:'已取消'};
async function api(path, data) {
  const response = await fetch(path, {method:data === undefined ? 'GET':'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:data === undefined ? undefined:JSON.stringify(data)});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || '请求失败');
  return result;
}
function notice(message) { $('notice').textContent = message; $('notice').hidden = !message; }
function node(tag, text, className) { const element=document.createElement(tag);element.textContent=text;if(className)element.className=className;return element; }

function cookieActions(task, actions, card, panel) {
  const view=node('button','查看 Cookie','quiet cookie-view'),download=node('button','下载 Cookie','quiet');
  view.type=download.type='button';view.disabled=panel.dataset.loading==='1';
  view.onclick=async()=>{
    if(panel.dataset.loading==='1')return;
    panel.dataset.loading='1';view.disabled=true;panel.hidden=false;panel.replaceChildren();
    const heading=node('div','','cookie-heading'),title=node('h4','正在读取 Cookie…'),close=node('button','收起','quiet');
    let dismissed=false;close.type='button';close.onclick=()=>{dismissed=true;panel.hidden=true;panel.replaceChildren();};
    heading.append(title,close);panel.append(heading,node('p','包含登录凭据，请勿外传。当前 HTTP 传输不加密。','hint'));
    try{
      const result=await api(`/api/admin/tasks/${task.id}/cookies`,{});
      if(dismissed||!panel.isConnected)return;
      title.textContent=`Cookie · ${result.count} 项`;
      panel.append(node('p','实时快照 · '+new Date(result.fetched_at*1000).toLocaleTimeString()+' · 如环境已关闭，请先「管理员打开」。','hint'));
      const data=node('pre',JSON.stringify(result.cookies,null,2),'cookie-json');data.tabIndex=0;data.setAttribute('aria-label','Cookie JSON');panel.append(data);
    }catch(error){if(!dismissed&&panel.isConnected){title.textContent='Cookie 读取失败';panel.append(node('p',error.message,'cookie-error'));}}
    finally{panel.dataset.loading='0';const current=panel.parentElement&&panel.parentElement.querySelector('.cookie-view');if(current)current.disabled=false;}
  };
  download.onclick=async()=>{
    if(!confirm('Cookie 可能包含登录凭据，当前 HTTP 传输不加密。确认下载并妥善保存？'))return;
    download.disabled=true;
    try{
      const response=await fetch(`/api/admin/tasks/${task.id}/cookies/download`,{method:'POST',headers:{'X-CSRF-Token':csrf}});
      if(!response.ok){const result=await response.json();throw new Error(result.error||'Cookie 下载失败');}
      const url=URL.createObjectURL(await response.blob()),link=document.createElement('a');link.href=url;link.download=`cookies-${task.code}.json`;document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);notice('Cookie JSON 已下载，请妥善保存，不要发送给他人。');
    }catch(error){notice(error.message);}finally{download.disabled=false;}
  };
  actions.append(view,download);card.append(panel);
}

if (document.body.dataset.page === 'admin') {
  if ($('login')) $('login').onsubmit=async event=>{event.preventDefault();try{await api('/api/login',{password:new FormData(event.target).get('password')});location.reload();}catch(error){notice(error.message);}};
  if ($('logout')) $('logout').onclick=async()=>{await api('/api/logout',{});location.reload();};
  if ($('create')) {
    $('create').onsubmit=async event=>{event.preventDefault();$('create-button').disabled=true;try{const values=Object.fromEntries(new FormData(event.target));await api('/api/admin/tasks',values);event.target.proxy.value='';notice('任务已创建，浏览器正在准备。');await refresh();}catch(error){notice(error.message);}finally{$('create-button').disabled=false;}};
    let last='';
    async function refresh() {
      try {
        const result=await api('/api/admin/tasks');const serialized=JSON.stringify(result.tasks);if(serialized===last)return;last=serialized;
        const cookiePanels=new Map([...$('tasks').querySelectorAll('.cookie-panel')].map(panel=>[Number(panel.dataset.taskId),panel]));
        $('count').textContent=result.tasks.length+' 个任务';$('tasks').replaceChildren();
        if(!result.tasks.length)$('tasks').append(node('p','还没有任务。创建后复制链接，即可交给对方。','muted'));
        for(const task of result.tasks){
          const card=node('article','','task-card'),top=node('div','','card-top');top.append(node('h3',task.title),node('span',labels[task.state],`badge ${task.state}`));card.append(top,node('div',task.code+' / '+new Date(task.created_at*1000).toLocaleString(),'task-code'),node('p',task.message,'muted'));
          card.append(node('p',task.proxy_label?'代理：'+task.proxy_label:'网络：服务器直连','hint'));
          const link=location.origin+'/t/'+task.token,row=node('div','','task-link'),input=document.createElement('input');input.value=link;input.readOnly=true;input.setAttribute('aria-label','任务链接');
          const copy=node('button','复制链接','quiet');copy.onclick=async()=>{try{await navigator.clipboard.writeText(link);notice('链接已复制');}catch{input.select();document.execCommand('copy');notice('链接已选中并复制');}};row.append(input,copy);card.append(row);
          const actions=node('div','','card-actions'),open=node('button','打开任务','quiet');open.onclick=()=>window.open(link,'_blank','noopener');actions.append(open);
          if(task.profile_id||task.state==='starting'){const adminOpen=node('button','管理员打开','quiet');adminOpen.onclick=()=>window.open(`/admin/tasks/${task.id}/browser`,'_blank','noopener');actions.append(adminOpen);}
          if(task.admin_open){const observing=['starting','active','verifying'].includes(task.state),adminClose=node('button',observing?'结束观察':'关闭管理员浏览器','quiet');adminClose.onclick=async()=>{try{const base=`/api/admin/tasks/${task.id}/browser`,{task:current}=await api(base);if(!['starting','active','verifying'].includes(current.state)&&!confirm('用户任务已结束。确认关闭管理员浏览器？环境及登录数据会保留。'))return;await api(base+'/close',{});await refresh();}catch(error){notice(error.message);}};actions.append(adminClose);}
          if(task.profile_id&&!['starting','active','verifying'].includes(task.state)){const reopen=node('button','重新开放','quiet');reopen.onclick=async()=>{if(confirm('重新开放原环境与原链接给原领取会话，并重新计算有效期。管理员将恢复为只读观察。继续？')){reopen.disabled=true;try{await api(`/api/admin/tasks/${task.id}/reopen`,{});notice('已重新开放，用户重新打开原链接即可继续');await refresh();}catch(error){notice(error.message);reopen.disabled=false;}}};actions.append(reopen);}
          if(['starting','active','verifying'].includes(task.state)){const cancel=node('button','取消任务','quiet');cancel.onclick=async()=>{if(confirm('取消后会锁定操作并关闭浏览器。继续？')){try{await api(`/api/admin/tasks/${task.id}/cancel`,{});await refresh();}catch(error){notice(error.message);}}};actions.append(cancel);}
          else if(task.profile_id){const release=node('button','释放环境','quiet');release.disabled=!!task.admin_open;release.onclick=async()=>{if(confirm('这会永久删除此任务的浏览器环境及登录数据，保留任务结果记录。确认释放？')){try{await api(`/api/admin/tasks/${task.id}/release`,{});notice('环境已释放，任务记录已保留');await refresh();}catch(error){notice(error.message);}}};actions.append(release);}
          if(!['starting','active','verifying'].includes(task.state)){const remove=node('button','删除任务','quiet danger');remove.disabled=!!task.admin_open;remove.onclick=async()=>{if(confirm(`永久删除任务 ${task.code}？任务记录、结果及剩余浏览器环境都将删除，原链接失效，无法恢复。`)){try{await api(`/api/admin/tasks/${task.id}/delete`,{confirm:true});notice('任务已删除，原链接已失效');await refresh();}catch(error){notice(error.message);}}};actions.append(remove);}
          card.append(actions);
          if(task.profile_id){const panel=cookiePanels.get(task.id)||node('section','','cookie-panel');if(!panel.dataset.taskId){panel.dataset.taskId=task.id;panel.hidden=true;}cookieActions(task,actions,card,panel);}
          $('tasks').append(card);
        }
      }catch(error){notice(error.message);}
    }
    refresh();setInterval(refresh,2500);
  }
} else {
  const isAdmin=document.body.dataset.page==='admin-browser',token=document.querySelector('meta[name="task-token"]').content;
  const base=isAdmin?'/api/admin/tasks/'+document.querySelector('meta[name="admin-task-id"]').content+'/browser':'/api/task/'+token;
  let authorized=false,operable=false,viewable=false,ended=false,submitting=false,adminStopped=false,streaming=false,fallbackReady=false,transportSwitching=false,inputBlocked=false,queue=Promise.resolve(),lastTabs='',textBatch=null,lastStreamGeneration=null,lastFrameWarning='',lastNetwork='',channel=null,channelReady=false,channelRetryAt=0,objectUrl='';
  const screen=$('screen'),inputPending=new Map(),httpInputs=new Set();
  const inputSource=Array.from(crypto.getRandomValues(new Uint32Array(4)),value=>value.toString(16).padStart(8,'0')).join('');
  let inputEpoch=0,inputSequence=0,commandId=0,moveBatch=null,pointer=null,lastClick=null,pendingMove=null,latestMove=null,moveTimer=0,moveFlight=null,moveRevision=0,moveSamples=0,moveSampleAt=0,composing=false;
  function releaseCapture(){const previous=pointer;pointer=null;if(previous&&screen.hasPointerCapture(previous.id))screen.releasePointerCapture(previous.id);}
  function invalidateInput(){
    inputEpoch++;textBatch=null;moveBatch=null;discardMove();moveFlight=null;lastClick=null;composing=false;releaseCapture();
    for(const item of inputPending.values()){clearTimeout(item.timer);item.reject(new Error('输入连接已结束'));}inputPending.clear();
    for(const controller of httpInputs)controller.abort();httpInputs.clear();
  }
  function updateConnection(){
    operable=authorized&&!transportSwitching&&!inputBlocked&&!submitting&&!adminStopped&&(channelReady||fallbackReady);
    for(const item of document.querySelectorAll('.toolbar input,.toolbar button,.input-bar input,.input-bar button,#tabs button'))item.disabled=!operable;
    if(!isAdmin)$('submit').disabled=!operable;
    const mode=channelReady?'live':viewable&&streaming?'fallback':'waiting';$('connection-badge').dataset.mode=mode;$('connection-state').textContent=inputBlocked?'输入已暂停':mode==='live'?'实时连接':mode==='fallback'?'兼容连接':adminStopped?'会话已退出':submitting||ended?'操作已锁定':viewable?'连接中':'等待连接';
  }
  function stopStream(clearBlocked=false){invalidateInput();streaming=false;channelReady=false;fallbackReady=false;transportSwitching=false;if(clearBlocked)inputBlocked=false;const old=channel;channel=null;if(old)old.close();screen.removeAttribute('src');if(objectUrl){URL.revokeObjectURL(objectUrl);objectUrl='';}updateConnection();}
  function startStream(){
    if(!viewable||channelReady||inputBlocked)return;
    if(!channel&&!inputBlocked&&!transportSwitching&&Date.now()>=channelRetryAt){
      const socket=new WebSocket((location.protocol==='https:'?'wss://':'ws://')+location.host+base+'/control');channel=socket;socket.binaryType='arraybuffer';
      let activation=null,latestTask=null,frameParts=[],frameSize=0,frameReceived=0,frameId=null,chunkedFrames=false;
      socket.onopen=()=>socket.send(JSON.stringify({csrf,frame_chunks:true}));
      socket.onmessage=event=>{
        if(channel!==socket)return;
        if(typeof event.data==='string'){
          const message=JSON.parse(event.data);
          if(message.type==='state'){
            latestTask=message.task;
            if(!channelReady&&authorized){
              if(activation)return;
              const interrupted=Boolean(pointer||httpInputs.size);transportSwitching=true;invalidateInput();const epoch=inputEpoch;updateConnection();
              activation=controlInput({type:'reset_input'},true).then(()=>{
                if(epoch!==inputEpoch)return;
                transportSwitching=false;
                if(channel===socket){channelReady=true;fallbackReady=false;streaming=true;renderState(latestTask);}else updateConnection();
              }).catch(()=>{
                if(epoch!==inputEpoch)return;
                transportSwitching=false;inputBlocked=true;updateConnection();notice('输入收尾未确认，请刷新任务页面后重试');
                if(channel===socket)socket.close();
              });
              if(interrupted)notice('连接方式已切换，未完成的输入已取消，请重新操作');
              return;
            }
            channelReady=true;fallbackReady=false;streaming=true;renderState(message.task);
          }
          else if(message.type==='frame_start'){
            if(frameSize||!Number.isSafeInteger(message.id)||message.id<0||!Number.isInteger(message.size)||message.size<1||message.size>2097152){blockInput('画面传输异常，操作已暂停，请刷新任务页面');return;}
            chunkedFrames=true;frameParts=[];frameSize=message.size;frameReceived=0;frameId=message.id;
          }
          else if(message.type==='input_ack'||message.type==='error'){
            const pending=inputPending.get(message.id);
            if(pending){inputPending.delete(message.id);clearTimeout(pending.timer);if(message.type==='input_ack')pending.resolve();else pending.reject(new Error(message.error||'输入未被接受'));}
            else if(message.type==='error')notice(message.error);
          }
        }else{
          let parts=[event.data];
          if(frameSize){
            if(event.data.byteLength<1||event.data.byteLength>16384||frameReceived+event.data.byteLength>frameSize){blockInput('画面传输异常，操作已暂停，请刷新任务页面');return;}
            frameParts.push(event.data);frameReceived+=event.data.byteLength;
            socket.send(JSON.stringify({type:'frame_ack',id:frameId,offset:frameReceived}));
            if(frameReceived<frameSize)return;
            parts=frameParts;frameParts=[];frameSize=0;frameReceived=0;frameId=null;
          }else if(chunkedFrames){blockInput('画面传输异常，操作已暂停，请刷新任务页面');return;}
          else socket.send(JSON.stringify({type:'frame_ack'}));
          if(viewable&&!submitting&&!adminStopped){const old=objectUrl;objectUrl=URL.createObjectURL(new Blob(parts,{type:'image/jpeg'}));$('screen').src=objectUrl;if(old)URL.revokeObjectURL(old);}
        }
      };
      socket.onclose=()=>{frameParts=[];frameSize=0;if(channel!==socket)return;if(channelReady)invalidateInput();channel=null;channelReady=false;streaming=false;updateConnection();channelRetryAt=Date.now()+3000;if(!ended&&!adminStopped)setTimeout(poll,100);};
      // Keep the HTTP transport available when an intermediary blocks WebSockets.
      setTimeout(()=>{if(channel===socket&&!channelReady&&!transportSwitching){channelRetryAt=Date.now()+3000;socket.close();}},2500);
    }
    if(!channel&&!streaming){streaming=true;fallbackReady=true;$('screen').src=base+'/stream?ts='+Date.now();}
    updateConnection();
  }
  function liveInput(){return channelReady&&channel&&channel.readyState===WebSocket.OPEN;}
  function blockInput(message){
    inputBlocked=true;invalidateInput();transportSwitching=false;fallbackReady=false;streaming=false;
    const old=channel;channel=null;channelReady=false;if(old)old.close();updateConnection();notice(message);
  }
  function cleanupFailed(epoch){if(epoch===inputEpoch){inputBlocked=true;updateConnection();notice('输入收尾未确认，请刷新任务页面后重试');}}
  function controlInput(action,forceHttp=false){
    const value={...action,generation:lastStreamGeneration,source:inputSource,sequence:++inputSequence};
    if(!forceHttp&&liveInput()){
      if(inputPending.size>=256){
        if(action.type==='reset_input'){channel.send(JSON.stringify({type:'input',action:value}));return Promise.resolve();}
        if(action.type!=='mouse_up')return Promise.reject(new Error('输入确认过慢，请稍后操作'));
      }
      const id=commandId=commandId%2147483646+1;
      return new Promise((resolve,reject)=>{
        const timer=setTimeout(()=>{inputPending.delete(id);blockInput('输入确认超时，操作已暂停。请刷新任务页面；未确认操作不会自动重发');reject(new Error('输入确认超时，请检查连接'));},5000);
        inputPending.set(id,{resolve,reject,timer});
        try{channel.send(JSON.stringify({type:'input',id,action:value}));}catch(error){inputPending.delete(id);clearTimeout(timer);reject(error);}
      });
    }
    const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),5000);httpInputs.add(controller);
    return fetch(base+'/input',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify(value),signal:controller.signal})
      .then(async response=>{const result=await response.json();if(!response.ok)throw new Error(result.error||'输入未被接受');return result;})
      .finally(()=>{clearTimeout(timer);httpInputs.delete(controller);});
  }
  function send(action,revision=null){
    if(!operable)return Promise.resolve(false);
    if(action.type==='text'&&textBatch&&textBatch.action.text.length+action.text.length<=10000){textBatch.action.text+=action.text;return textBatch.promise;}
    if(action.type==='mouse_move'&&moveBatch){Object.assign(moveBatch.action,action);return moveBatch.promise;}
    const epoch=inputEpoch,batch={action:{...action},promise:null,resolve:null};
    batch.promise=new Promise(resolve=>{batch.resolve=resolve;});textBatch=action.type==='text'?batch:null;moveBatch=action.type==='mouse_move'?batch:null;
    queue=queue.catch(()=>{}).then(()=>{
      if(textBatch===batch)textBatch=null;if(moveBatch===batch)moveBatch=null;
      if(!operable||epoch!==inputEpoch||revision!==null&&revision!==moveRevision){batch.resolve(false);return;}
      const live=liveInput(),receipt=controlInput(batch.action).then(()=>batch.resolve(true)).catch(error=>{
        batch.resolve(false);if(epoch===inputEpoch){
          notice(error.name==='AbortError'?'输入连接超时或已中断':error.message);
          if(batch.action.type==='mouse_up'){lastClick=null;controlInput({type:'reset_input'},!live).catch(()=>cleanupFailed(epoch));}
          else if(batch.action.type==='reset_input')cleanupFailed(epoch);
          else cancelPointer();
        }
      });
      // WebSocket preserves send order. Await receipts at the caller, not each mouse frame.
      if(!live)return receipt;
    });
    return batch.promise;
  }
  $('claim').onclick=async()=>{try{await api(base+(isAdmin?'/open':'/claim'),{});await state();}catch(error){notice(error.message);}};
  $('submit').onclick=async()=>{
    if(isAdmin){try{const {task}=await api(base);if(!['starting','active','verifying'].includes(task.state)&&!confirm('用户任务已结束。确认关闭管理员浏览器？环境及登录数据会保留。'))return;adminStopped=true;operable=false;viewable=false;stopStream();await api(base+'/close',{});ended=true;const {task:current}=await api(base),observing=['starting','active','verifying'].includes(current.state);showOverlay(observing?'已结束观察':'管理员浏览器已关闭',observing?'管理员已退出观察，用户继续操作，浏览器未关闭。再次观察请刷新此页。':'用户任务已结束，浏览器已关闭，环境及登录数据保留。重新打开请刷新此页。');$('admin-mode').textContent='管理员会话未打开';$('submit').disabled=true;}catch(error){adminStopped=false;ended=false;notice(error.message);await state();}return;}
    if(!confirm('完成后立即收回操作权限，只有管理员重新开放后才能继续。确认完成？'))return;
    submitting=true;operable=false;viewable=false;stopStream();$('submit').disabled=true;showOverlay('正在完成任务','操作权限正在收回。');
    try{await api(base+'/submit',{});submitting=false;await state();}catch(error){submitting=false;notice(error.message);await state();}
  };
  function showOverlay(title,message){$('overlay').hidden=false;$('overlay-title').textContent=title;$('overlay-message').textContent=message;$('claim').hidden=true;}
  async function state(){
    const {task}=await api(base);if(!channelReady)renderState(task);
  }
  function renderTabs(tabs){
    const serialized=JSON.stringify([tabs,operable]);if(serialized===lastTabs)return;lastTabs=serialized;$('tabs').replaceChildren();
    for(const tab of tabs){
      let label='新标签页';if(tab.url!=='about:blank'){try{label=new URL(tab.url).hostname.replace(/^www\./,'')||'网页标签';}catch{label=tab.url||'网页标签';}}
      const item=node('div','','browser-tab'+(tab.active?' active':'')),select=node('button','','tab-select'),close=node('button','×','tab-close');
      select.type='button';select.setAttribute('role','tab');select.setAttribute('aria-selected',String(tab.active));select.setAttribute('aria-label',label);select.title=tab.url;select.disabled=!operable;
      select.append(node('span',tab.url==='about:blank'?'◌':label[0],'tab-mark'),node('span',label,'tab-text'));
      select.onclick=()=>{flushMove();send(Number.isInteger(tab.id)?{type:'tab',id:tab.id}:{type:'tab',index:tab.index});};
      close.type='button';close.title='关闭标签页';close.setAttribute('aria-label','关闭 '+label+' 标签页');close.disabled=!operable||!Number.isInteger(tab.id);
      close.onclick=event=>{event.stopPropagation();flushMove();send({type:'close_tab',id:tab.id});};item.append(select,close);$('tabs').append(item);
      if(tab.active&&document.activeElement!==$('address'))$('address').value=tab.url==='about:blank'?'':tab.url;
    }
    const active=$('tabs').querySelector('.active');if(active){const left=active.offsetLeft,right=left+active.offsetWidth;if(left<$('tabs').scrollLeft)$('tabs').scrollLeft=left;else if(right>$('tabs').scrollLeft+$('tabs').clientWidth)$('tabs').scrollLeft=right-$('tabs').clientWidth;}
  }
  function renderState(task){
    if(submitting||adminStopped)return;authorized=isAdmin?task.can_control:task.state==='active'&&task.mine;
    viewable=isAdmin?task.browser_ready&&task.admin_open:authorized;
    if(task.stream_generation!==lastStreamGeneration){stopStream(true);lastStreamGeneration=task.stream_generation;}
    if(viewable)startStream();else stopStream(true);
    updateConnection();
    if(lastFrameWarning&&!task.frame_warning&&$('notice').textContent===lastFrameWarning)notice('');lastFrameWarning=task.frame_warning||'';
    $('status').textContent=task.message+(isAdmin?' · '+labels[task.state]:' · 有效期至 '+new Date(task.expires_at*1000).toLocaleTimeString());$('submit').disabled=isAdmin?!task.admin_open:!operable;
    $('task-state').textContent=isAdmin&&task.admin_open?(operable?'管理员操作':'只读观察'):labels[task.state]||'连接中';$('task-state').dataset.state=task.state;
    renderNetwork(viewable?task.network_warning:null);
    if(isAdmin)$('submit').textContent=['starting','active','verifying'].includes(task.state)?'结束观察':'关闭管理员浏览器';
    for(const item of document.querySelectorAll('.toolbar input,.toolbar button,.input-bar input,.input-bar button'))item.disabled=!operable;
    if(viewable){$('overlay').hidden=true;if(task.input_error||task.frame_warning)notice(task.input_error||task.frame_warning);renderTabs(task.tabs||[]);}
    else if(isAdmin){showOverlay(task.admin_open?'正在打开浏览器':'管理员浏览器',task.browser_message);if(!task.admin_open&&task.can_open){$('claim').textContent='打开原浏览器 →';$('claim').hidden=false;}}
    else{showOverlay(labels[task.state]||'任务状态',task.message);if(task.state==='active'&&!task.claimed){$('claim').hidden=false;$('overlay-title').textContent='浏览器已准备好';}ended=!['starting','active','verifying'].includes(task.state);}
    if(isAdmin)$('admin-mode').textContent=!task.admin_open?'管理员会话未打开':operable?'管理员操作模式 · 用户原链接已锁定':'只读观察 · 用户操作中，管理员不可输入';
    if(!operable)for(const item of $('tabs').querySelectorAll('button'))item.disabled=true;
    updateConnection();
  }
  function renderNetwork(warning){
    const serialized=JSON.stringify(warning||null);if(serialized===lastNetwork)return;lastNetwork=serialized;
    const alert=$('network-alert');alert.hidden=!warning;
    if(!warning){alert.open=false;$('network-errors').replaceChildren();$('network-message').textContent='';return;}
    alert.dataset.level=warning.level;$('network-message').setAttribute('aria-live',warning.level==='error'?'assertive':'polite');$('network-message').textContent=warning.message;
    $('network-count').textContent=warning.count+' 项';$('network-limit').hidden=!warning.limited;$('network-errors').replaceChildren();
    const kinds={document:'页面',script:'脚本',stylesheet:'样式',xhr:'接口',fetch:'接口',image:'图片',font:'字体',media:'音视频',websocket:'实时请求',eventsource:'实时请求'};
    const reasons={'net::ERR_SOCKS_CONNECTION_FAILED':'SOCKS5 连接失败','net::ERR_PROXY_CONNECTION_FAILED':'代理连接失败','net::ERR_TUNNEL_CONNECTION_FAILED':'代理隧道连接失败','net::ERR_NAME_NOT_RESOLVED':'域名解析失败','net::ERR_CONNECTION_TIMED_OUT':'连接超时','net::ERR_TIMED_OUT':'请求超时','net::ERR_CONNECTION_RESET':'连接被重置',NETWORK_ERROR:'网络请求失败'};
    for(const error of warning.details||[]){const row=node('li');row.append(node('span',kinds[error.type]||'资源','resource-kind'),node('span',error.host||'未知域名','resource-host'),node('span',reasons[error.reason]||error.reason,'resource-reason'));$('network-errors').append(row);}
  }
  async function poll(){if(channelReady)return;try{await state();}catch(error){if(authorized&&fallbackReady)inputBlocked=true;operable=false;viewable=false;stopStream();notice(error.message+(inputBlocked?' · 请刷新任务页面后重试':''));}finally{if(!ended&&!channelReady)setTimeout(poll,500);}}
  function reconnectStream(){if(!streaming)return;streaming=false;setTimeout(startStream,500);}
  screen.onerror=()=>{if(!channelReady)reconnectStream();};
  window.addEventListener('pagehide',()=>{
    if(operable){const action={type:'reset_input',generation:lastStreamGeneration,source:inputSource,sequence:++inputSequence};if(liveInput())channel.send(JSON.stringify({type:'input',action}));else fetch(base+'/input',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify(action),keepalive:true}).catch(()=>{});}
    stopStream();
  });
  function coords(event){const rect=screen.getBoundingClientRect();return{x:Math.min(1279,Math.max(0,(event.clientX-rect.left)*1280/rect.width)),y:Math.min(799,Math.max(0,(event.clientY-rect.top)*800/rect.height))};}
  function modifiers(event){return[event.shiftKey&&'Shift',event.altKey&&'Alt',event.ctrlKey&&'Control',event.metaKey&&'Meta'].filter(Boolean);}
  function discardMove(){clearTimeout(moveTimer);moveTimer=0;pendingMove=latestMove=null;moveSamples=0;moveSampleAt=performance.now();moveRevision++;}
  function scheduleMove(){
    if(!operable||!latestMove||moveTimer||moveSamples>=100&&moveFlight)return;
    if(moveSamples>=100)moveSamples=0;
    moveTimer=setTimeout(sampleMove,Math.max(0,20-(performance.now()-moveSampleAt)));
  }
  function sampleMove(){
    moveTimer=0;if(!operable||!latestMove)return;
    pendingMove=latestMove;latestMove=null;moveSamples++;moveSampleAt=performance.now();drainMove();
  }
  function drainMove(){
    if(moveFlight||!operable)return;
    if(latestMove){scheduleMove();return;}
    const sample=pendingMove;pendingMove=null;
    if(!sample||sample.epoch!==inputEpoch||sample.revision!==moveRevision)return;
    const record={epoch:inputEpoch};moveFlight=record;
    send(sample.action,sample.revision).then(()=>{
      if(moveFlight!==record)return;moveFlight=null;
      if(record.epoch!==inputEpoch)return;
      // Cap work during a stalled receipt; resume with the newest real event, not its history.
      if(moveSamples>=100)moveSamples=0;
      if(latestMove)scheduleMove();else drainMove();
    });
  }
  function flushMove(){
    const sample=latestMove||pendingMove;discardMove();
    if(sample&&!moveFlight&&operable){pendingMove={...sample,epoch:inputEpoch,revision:moveRevision};drainMove();}
  }
  function cancelPointer(){discardMove();if(!pointer)return;lastClick=null;releaseCapture();send({type:'reset_input'});}
  screen.draggable=false;screen.ondragstart=event=>event.preventDefault();screen.oncontextmenu=event=>event.preventDefault();
  screen.onpointerdown=event=>{
    if(!operable||pointer||![0,1,2].includes(event.button))return;
    event.preventDefault();flushMove();screen.focus({preventScroll:true});
    const point=coords(event),button=['left','middle','right'][event.button],now=performance.now();
    const count=lastClick&&lastClick.button===button&&now-lastClick.at<500&&Math.hypot(point.x-lastClick.x,point.y-lastClick.y)<4?Math.min(3,lastClick.count+1):1;
    pointer={id:event.pointerId,button,count,...point,moved:false};screen.setPointerCapture(event.pointerId);
    send({type:'mouse_down',...point,button,click_count:count,modifiers:modifiers(event)});
  };
  screen.onpointermove=event=>{
    if(!operable||pointer&&pointer.id!==event.pointerId)return;
    const point=coords(event);if(pointer&&Math.hypot(point.x-pointer.x,point.y-pointer.y)>=4)pointer.moved=true;
    latestMove={action:{type:'mouse_move',...point,modifiers:modifiers(event)},epoch:inputEpoch,revision:moveRevision};scheduleMove();
  };
  function pointerUp(event){
    if(!pointer||pointer.id!==event.pointerId)return;
    event.preventDefault();flushMove();const previous=pointer,point=coords(event);releaseCapture();
    send({type:'mouse_up',...point,button:previous.button,click_count:previous.count,modifiers:modifiers(event)});
    lastClick=previous.moved?null:{...point,button:previous.button,count:previous.count,at:performance.now()};
  }
  screen.onpointerup=pointerUp;window.addEventListener('pointerup',pointerUp);
  screen.onpointercancel=cancelPointer;screen.onlostpointercapture=event=>{if(pointer&&pointer.id===event.pointerId)cancelPointer();};
  screen.onblur=cancelPointer;window.addEventListener('blur',cancelPointer);
  screen.addEventListener('wheel',event=>{
    event.preventDefault();flushMove();
    const dx=event.deltaX*(event.deltaMode===1?16:event.deltaMode===2?1280:1),dy=event.deltaY*(event.deltaMode===1?16:event.deltaMode===2?800:1);
    send({type:'wheel',...coords(event),dx:Math.max(-2000,Math.min(2000,dx)),dy:Math.max(-2000,Math.min(2000,dy)),modifiers:modifiers(event)});
  },{passive:false});
  screen.onkeydown=event=>{
    if(!operable||composing||event.isComposing||event.keyCode===229||['Dead','Unidentified'].includes(event.key))return;
    const altGraph=event.getModifierState('AltGraph'),command=(event.ctrlKey||event.metaKey)&&!altGraph;
    const primary=command&&!event.altKey&&['a','c','x','z','y'].includes(event.key.toLowerCase());
    if(command&&event.key.toLowerCase()==='v')return;
    event.preventDefault();
    flushMove();
    if(Array.from(event.key).length===1&&(!event.ctrlKey&&!event.metaKey&&!event.altKey||altGraph))send({type:'text',text:event.key});
    else if(!['Control','Alt','Shift','Meta','AltGraph'].includes(event.key)){let key=event.key===' '?'Space':event.key;key=(primary?'Control+':(event.ctrlKey?'Control+':'')+(event.metaKey?'Meta+':''))+(event.altKey?'Alt+':'')+(event.shiftKey?'Shift+':'')+key;send({type:'key',key,primary});}
  };
  screen.onpaste=event=>{event.preventDefault();flushMove();if(operable&&event.clipboardData)send({type:'text',text:event.clipboardData.getData('text')});};
  screen.addEventListener('compositionstart',()=>{composing=true;});
  screen.addEventListener('compositionend',event=>{composing=false;if(event.data){flushMove();send({type:'text',text:event.data});}});
  $('navigate').onsubmit=event=>{event.preventDefault();flushMove();send({type:'navigate',url:$('address').value});};
  $('back').onclick=()=>{flushMove();send({type:'back'});};$('reload').onclick=()=>{flushMove();send({type:'reload'});};$('new-tab').onclick=()=>{flushMove();send({type:'new_tab'});};
  $('text-input').onsubmit=async event=>{event.preventDefault();flushMove();const value=$('remote-text').value;if(await send({type:'text',text:value})){if($('remote-text').value===value)$('remote-text').value='';screen.focus();}};
  const clearInput=node('button','清空远程输入框','quiet');clearInput.id='clear-input';clearInput.type='button';clearInput.disabled=true;
  clearInput.title='先点击远程网页中的输入框，再点击此按钮清空内容';$('input-actions').append(clearInput);
  clearInput.onclick=()=>{
    if(!operable)return;flushMove();
    const epoch=inputEpoch;textBatch=null;
    queue=queue.catch(()=>{}).then(async()=>{
      if(!operable||epoch!==inputEpoch)return;
      await controlInput({type:'key',key:'Control+a'});
      if(!operable||epoch!==inputEpoch)return;
      await controlInput({type:'key',key:'Backspace'});
      if(operable&&epoch===inputEpoch)screen.focus();
    }).catch(error=>{if(epoch===inputEpoch)notice(error.message);});return queue;
  };
  if(isAdmin)api(base+'/open',{}).catch(error=>notice(error.message)).finally(()=>poll());else poll();
}
