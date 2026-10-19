from .integration_core import *


def homepage_documents(
    site: IntegrationSite, compose: str, services: str, ip: str, token: str
) -> tuple[str, str]:
    """The two Homepage documents with this application's card added.

    The compose edit is one ``extra_hosts`` entry mapping the tailnet name to
    the tailnet address, because the container resolves neither. It is refused
    unless there is exactly one Homepage service and it has no ``extra_hosts``
    of its own: merging into an existing mapping is a decision an operator
    makes, not one a tool makes silently. A site with a ``container_origin``
    gets no compose edit at all: the container never uses the tailnet name,
    and whatever routes it to that origin belongs to the operator.
    """
    if site.marker in compose or site.marker in services:
        raise IntegrationError("unowned Homepage configuration carries our marker")
    if any(heading + ":" in services for heading in site.owned_group_headings):
        raise IntegrationError("unowned Homepage group heading")
    if site.container_origin is None:
        anchor = f"    container_name: {site.homepage_container}\n"
        if compose.count(anchor) != 1 or "extra_hosts:" in compose:
            raise IntegrationError(
                "Homepage compose needs an explicitly reviewed host mapping"
            )
        compose = compose.replace(
            anchor,
            anchor
            + f'    {site.marker}\n    extra_hosts:\n      - "{site.host}:{ip}"\n',
        )
    # A JSON quoted scalar is also a YAML scalar. Only a summary token enters
    # Homepage; it cannot authorize browser reads or connector writes.
    services += f"""\n{site.marker}
- {site.group_heading}:
    - {site.entry_name}:
        icon: {site.entry_icon}
        href: {site.public_origin}
        description: {site.entry_description}
        widget:
          type: customapi
          url: {site.widget_origin}{site.summary_path}
          refreshInterval: 15000
          headers:
            Authorization: {json.dumps("Bearer " + token)}
          mappings:
            - field: reason
              label: Status
              format: text
            - field: freshness.total
              label: Observed
              format: number
            - field: counts.by_state.running
              label: Running
              format: number
"""
    return compose, services


def container_access_checks(
    site: IntegrationSite,
) -> tuple[tuple[str, str | None, int], ...]:
    """The four requests the container makes, and the answer each one must get.

    Named rather than written into the script below, because this is a contract
    between two modules and a contract embedded in a JavaScript string literal
    cannot be asserted against the application that has to honour it.
    ``tests/test_workbench_http.py`` drives every row of this table through
    :class:`~asl_workbench.http.Application`, so a change to the service's
    boundary fails there rather than inside a container at cutover.

    Each row is a different question. A summary credential reads the summary; an
    unaccepted one is told it was not accepted rather than shown a wall; the
    summary credential may not read owner data and is told *that* rather than
    that the route does not exist; and the health route a monitor polls needs no
    credential at all.
    """
    return (
        (site.summary_path, "token", 200),
        (site.summary_path, "invalid", 401),
        (site.owner_only_path, "token", 403),
        (site.health_path, None, 200),
    )


def container_access_script(site: IntegrationSite) -> str:
    """The check the Homepage container itself runs against the service.

    The four requests of :func:`container_access_checks`, made from inside the
    container that will be making them. Verifying from the host would prove
    something about the host's network, which is not the thing that has to work.
    """
    checks = json.dumps([list(row) for row in container_access_checks(site)])
    return (
        "const p=JSON.parse(require('fs').readFileSync(0,'utf8'));\n"
        f"const checks={checks};\n"
        "(async()=>{for(const [path,kind,status] of checks) {\n"
        "const token=kind==='token'?p.token:kind;\n"
        "const r=await fetch(p.origin+path,{headers:token?"
        "{Authorization:'Bearer '+token}:{},signal:AbortSignal.timeout(10000)});\n"
        "if(r.status!==status)throw Error('access verification failed');\n"
        f"if(path==={json.dumps(site.summary_path)}&&r.status===200)"
        "{const d=await r.json();"
        "if(!d.counts||!d.freshness||'runs' in d)"
        "throw Error('summary shape failed');}\n"
        "}console.log('Homepage container access verified');})()"
        ".catch(()=>{process.exitCode=1});"
    )


# -- ntfy access control ----------------------------------------------------


def acl_permissions(acl: str, user: str) -> list[str] | None:
    """The permission lines ``ntfy user list`` prints for one account."""
    blocks = re.split(r"(?m)^user ", acl)
    block = next((item for item in blocks if item.startswith(user + " (")), None)
    if block is None:
        return None
    if not block.startswith(user + " (role: user,"):
        raise IntegrationError("publisher role is unsafe")
    return [line.strip() for line in block.splitlines()[1:] if line.strip()]


def check_anonymous(site: IntegrationSite, acl: str) -> None:
    """Refuse a server where anonymous callers already hold any topic grant.

    The publisher is write-only on one topic, which is only meaningful if the
    topic is not readable by everyone. This is checked on the server as it is,
    and never changed: the ACL is shared with whatever else uses this ntfy.
    """
    anonymous = next(
        (
            block
            for block in re.split(r"(?m)^user ", acl)
            if block.startswith("* (role: anonymous,")
        ),
        "",
    )
    lines = [line.strip() for line in anonymous.splitlines()[1:] if line.strip()]
    if tuple(lines) != EMPTY_ACL_PERMISSIONS:
        raise IntegrationError(
            "ntfy anonymous access must already be default-deny without topic grants"
        )
    del site  # the anonymous rule is the server's, not this site's


def check_publisher(
    site: IntegrationSite,
    acl: str,
    *,
    allow_absent: bool = False,
    allow_empty: bool = False,
) -> None:
    """Refuse a publisher account that is broader than the one topic.

    ``allow_empty`` is for the window during an apply where the account exists
    and its grant has not been made yet; ``allow_absent`` is for a remove that
    finds the account already gone. Neither relaxes what a *broadened* account
    means, which is that somebody repurposed it and it is no longer ours to
    delete.
    """
    permissions = acl_permissions(acl, site.publisher_user)
    if permissions is None:
        if allow_absent:
            return
        raise IntegrationError("publisher account is absent")
    expected = [f"- write-only access to topic {site.publisher_topic}"]
    if permissions in (expected, expected + [EMPTY_ACL_PERMISSIONS[-1]]):
        return
    if allow_empty and tuple(permissions) == EMPTY_ACL_PERMISSIONS:
        return
    raise IntegrationError("publisher permissions differ from the dedicated topic")


# -- Uptime Kuma ------------------------------------------------------------

#: The adapter, run by ``node`` inside the Kuma container with its payload on
#: stdin. Everything host-specific -- the monitor name, the names an installed
#: monitor may still carry, the health URL, the login -- arrives in the payload,
#: so this text is the logic and nothing else. The password arrives the same
#: way and never appears in an argument list or a container's environment.
KUMA_SCRIPT = r"""
const {io}=require('/app/node_modules/socket.io-client');
const p=global.PAYLOAD;
const socket=io('http://127.0.0.1:3001',{transports:['websocket'],reconnection:false,timeout:10000});
const deadline=setTimeout(()=>{socket.disconnect();process.exitCode=1},45000);
let monitors={};
socket.on('monitorList',v=>{monitors=v});
const ready=new Promise(resolve=>socket.once('info',resolve));
const call=(event,...args)=>new Promise((resolve,reject)=>{
 socket.timeout(12000).emit(event,...args,(error,r)=>
  error||!r?.ok?reject(Error('Kuma refused: '+event)):resolve(r));
});
const definition={name:p.name,type:'json-query',
 url:p.url,method:'GET',jsonPath:'status',
 jsonPathOperator:'==',expectedValue:'ok',interval:60,retryInterval:60,
 resendInterval:0,maxretries:2,timeout:10,maxredirects:0,ignoreTls:false,
 notificationIDList:{},saveResponse:false,saveErrorResponse:false,
 accepted_statuscodes:['200'],authMethod:null,headers:'',body:null,
 upsideDown:false,expiryNotification:false,domainExpiryNotification:false,
 parent:null,retryOnlyOnStatusCodeFailure:false,responseMaxLength:1024,
 dns_resolve_type:'A',dns_resolve_server:'',proxyId:null,httpBodyEncoding:'json',
 kafkaProducerBrokers:[],kafkaProducerSaslOptions:{mechanism:'None'},
 rabbitmqNodes:[],conditions:[]};
(async()=>{
 await ready;
 await call('login',{username:p.username,password:p.password});
 await call('getMonitorList');
 // An owned monitor may still carry the name it was installed under, so it is
 // matched on any known name and renamed by the edit below. An unowned one is
 // never adopted, whichever name it carries.
 const matches=Object.values(monitors).filter(m=>p.names.includes(m.name));
 if(matches.length>1||matches.length&&!p.owned)
  throw Error('Monitor ownership conflict');
 const old=matches[0];
 let full=old?(await call('getMonitor',old.id)).monitor:null;
 if(full&&(full.type!==definition.type||full.url!==definition.url))
  throw Error('Monitor repurposed');
 if(p.mode==='remove'){
   if(old)await call('deleteMonitor',old.id);
 }else if(p.mode==='apply'){
   // No notification is attached here. Activation waits for local health, so
   // preparing integration before production cannot generate a false outage.
   let healthy=false;
   try{const r=await fetch(definition.url,{signal:AbortSignal.timeout(5000)});
     healthy=r.ok&&(await r.json()).status==='ok';}catch{}
   if(old)await call('editMonitor',{...full,...definition});
   else {const r=await call('add',{...definition,active:false});
     full={id:r.monitorID};}
   if(healthy)await call('resumeMonitor',full.id);
   else await call('pauseMonitor',full.id);
 }else if(p.mode==='verify'){
   if(!full||!full.active||Object.entries(definition).some(
     ([k,v])=>JSON.stringify(full[k])!==JSON.stringify(v)))
     throw Error('Monitor differs');
 }
 console.log(JSON.stringify({ok:true,monitor:definition.name,mode:p.mode}));
})().catch(error=>{console.error(
  error.message.startsWith('Kuma refused: ')?error.message
  :'Kuma integration refused');process.exitCode=1})
 .finally(()=>{clearTimeout(deadline);socket.disconnect()});
"""


# -- Receipts ---------------------------------------------------------------
