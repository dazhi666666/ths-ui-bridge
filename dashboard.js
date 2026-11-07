const $=id=>document.getElementById(id);
const labels={executed:'已执行',dry_run:'仅预演',hold:'观望',signal_only:'待执行信号',duplicate:'重复已忽略',intent_rejected:'规则拒绝',order_open:'盯盘手订单已接收',order_filled:'盯盘手订单已成交',order_partially_filled:'盯盘手部分成交',order_cancelled:'已撤单',needs_confirmation:'需人工确认',analysis_failed:'分析失败'};
function safe(value){return String(value??'—').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function tagClass(state,error){return error?'bad':['executed','hold','order_filled'].includes(state)?'good':''}
function localTime(value){
  if(!value)return '—';
  const parsed=new Date(value.replace(' ','T')+'Z');
  if(Number.isNaN(parsed.getTime()))return value;
  return parsed.toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});
}
async function refresh(){
  $('refreshBtn').disabled=true;
  try{
    const [statusRes,eventsRes]=await Promise.all([fetch('/api/status',{cache:'no-store'}),fetch('/api/events',{cache:'no-store'})]);
    const status=await statusRes.json(),events=await eventsRes.json();
    $('serviceText').textContent='服务运行中';
    $('liveMode').textContent=status.auto_execute?'已开启':(status.live?'授权未完成':'已关闭');
    const device=status.device;
    $('deviceState').textContent=device?'已连接':'连接异常';
    $('deviceMeta').textContent=device?`${device.device.productName} · Android ${device.device.sdkInt}`:(status.device_error||'无法连接');
    $('appState').textContent=device?.ths_running?'前台运行':'未在前台';
    $('appMeta').textContent=device?.simulation_home_visible?'模拟练习区':device?.simulation_order_page_visible?'模拟炒股委托页':'请进入模拟交易';
    const hook=status.webhook;
    $('hookState').textContent=hook?.receiver_online?(hook.last_delivery_at?'已收到投递':'等待投递'):'接收器离线';
    $('hookMeta').textContent=hook?.last_delivery_at?`最近 ${localTime(hook.last_delivery_at)}`:'接收器在线 · 尚无正式事件';
    const rows=events.items||[];
    $('eventRows').innerHTML=rows.length?rows.map(e=>`<tr><td>${safe(localTime(e.received_at))}</td><td><strong>${safe(e.symbol)}</strong></td><td>${safe(e.action)}</td><td>${safe(e.validation_status)}</td><td><span class="tag ${tagClass(e.state,e.error)}">${safe(e.error?'已阻止':labels[e.state]||e.state)}</span></td></tr>`).join(''):'<tr><td colspan="5" class="empty">还没有接收到事件</td></tr>';
    $('lastUpdate').textContent=`最后更新 ${new Date().toLocaleTimeString('zh-CN',{hour12:false})}`;
  }catch(error){$('serviceText').textContent='连接失败';$('deviceState').textContent='无法读取';}
  finally{$('refreshBtn').disabled=false}
}
$('refreshBtn').addEventListener('click',refresh);refresh();setInterval(refresh,10000);
