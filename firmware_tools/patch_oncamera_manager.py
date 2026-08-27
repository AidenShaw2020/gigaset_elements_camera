"""Install the local manager into a camera-owned Gigaset Camera MEF image.

The cramfs image is patched in place: inode sizes and filesystem layout remain
fixed, every replacement must fit the original compressed block, and both the
cramfs and MEF checksums are recalculated before an output is written.
"""

from __future__ import annotations

import argparse
import base64
import re
import struct
import zlib
from pathlib import Path

import cramfs_extract
from patch_cramfs_inplace import patch_file_many


DEFAULT_PAGE = b"""<html><head><link rel=icon href=data:,><meta http-equiv=refresh content="0;URL=/en/main.asp<% getProxyLinkQ(); %>"></head></html>"""

DASHBOARD = b"""<html><head><style>body{font:14px Arial;margin:40px}a{display:inline-block;width:130px;margin:4px;padding:16px;background:#eee;text-align:center}</style></head><body><h1>Gigaset camera</h1><h3>LOCAL MODE / cloud: <% getConfTxt("sys","otproxyc","ENABLE"); %></h3><a href=motiondect2.asp?p=l>Live video</a><a href=motiondect2.asp?p=i>Image</a><a href=motiondect2.asp?p=s>Streams</a><a href=motiondect2.asp?p=n>Network</a><a href=motiondect2.asp?p=w>Wi-Fi</a><a href=motiondect2.asp?p=m>Motion</a><a href=motiondect2.asp?p=c>Cloud / proxy</a><a href=motiondect.htm>Home Assistant</a><a href=/cgi-bin/video.asp>Security</a><a href=motiondect2.asp?p=d>Storage</a><a href=motiondect2.asp?p=y>System</a></body></html>"""

MOTION_PAGE = b"""<html><head><meta charset=windows-1252><meta http-equiv=pragma content=no-cache><link rel=icon href="data:,"><style>body{font:14px Arial;margin:20px;background:#eef2f3;color:#234}a{color:#176b87}.layout{display:flex;gap:22px;flex-wrap:wrap}.view{position:relative;width:640px;height:480px;background:#222;cursor:crosshair}.view img{width:640px;height:480px}.zone{position:absolute;border:3px solid #b7db45;background:rgba(183,219,69,.15);box-sizing:border-box;pointer-events:none}.panel{background:white;padding:18px;border-radius:8px;min-width:290px;box-shadow:0 2px 8px #999}label{display:grid;grid-template-columns:120px 1fr;align-items:center;margin:8px 0}input[type=number]{width:85px}button,input[type=submit]{padding:8px 16px;margin-top:10px}.msg{color:#690}</style><script src=../../cab/swfobject.js></script></head><body onload=init()><p><a href=main.asp>&larr; Back</a></p><h1>Motion detection</h1><p>Choose a window, then drag over the image to mark its active area.</p><div class=msg><% getMessage(); %></div><form method=post action="/form/motiondectOtherApply"><div class=layout><div class=view><img id=cam src="/snapshot.jpg"><div id=zone class=zone></div></div><div class=panel><label>Window <select id=wid name=WIN_ID onchange=go()><option value=0 <% motionWinSelect("0"); %>>1<option value=1 <% motionWinSelect("1"); %>>2<option value=2 <% motionWinSelect("2"); %>>3<option value=3 <% motionWinSelect("3"); %>>4</select></label><label>Enabled <span><input type=radio name=MVWINDOW value=enable <% getMotionCheck("sys", "MVWINDOW", "enable"); %>> Yes <input type=radio name=MVWINDOW value=disable <% getMotionCheck("sys", "MVWINDOW", "disable"); %>> No</span></label><label>Threshold <input type=number name=MVTHRESHOLD min=1 max=100 value="<% getMotionTxt("sys", "MVTHRESHOLD"); %>"></label><label>Sensitivity <input type=number name=MVSENSITIVITY min=10 max=100 value="<% getMotionTxt("sys", "MVSENSITIVITY"); %>"></label><label>Left <input id=sx type=number name=MVSTARTX min=0 max=639 value="<% getMotionTxt("sys", "MVSTARTX"); %>"></label><label>Top <input id=sy type=number name=MVSTAETY min=0 max=479 value="<% getMotionTxt("sys", "MVSTAETY"); %>"></label><label>Right <input id=ex type=number name=MVENDX min=0 max=639 value="<% getMotionTxt("sys", "MVENDX"); %>"></label><label>Bottom <input id=ey type=number name=MVENDY min=0 max=479 value="<% getMotionTxt("sys", "MVENDY"); %>"></label><button type=button onclick=shot()>Refresh image</button> <input type=submit value=Apply><p>Threshold: 1-100<br>Sensitivity: 10-100</p></div></div></form></body></html>"""

MOTION_JS = b"""function el(x){return document.getElementById(x)}function go(){location=location.pathname+'?winid='+el('wid').value}var drag=0,start;function xy(e){var r=document.querySelector('.view').getBoundingClientRect();return[Math.max(0,Math.min(639,Math.round((e.clientX-r.left)*640/r.width))),Math.max(0,Math.min(479,Math.round((e.clientY-r.top)*480/r.height)))]}function zone(){var a=['sx','sy','ex','ey'].map(function(x){return Number(el(x).value)}),z=el('zone'),q=document.querySelector('input[name=MVWINDOW]:checked');z.style.display=q&&q.value=='enable'?'block':'none';z.style.left=a[0]/6.4+'%';z.style.top=a[1]/4.8+'%';z.style.width=(a[2]-a[0]+1)/6.4+'%';z.style.height=(a[3]-a[1]+1)/4.8+'%'}function down(e){drag=1;start=xy(e);move(e);e.preventDefault()}function move(e){if(!drag)return;var p=xy(e);el('sx').value=Math.min(start[0],p[0]);el('sy').value=Math.min(start[1],p[1]);el('ex').value=Math.max(start[0],p[0]);el('ey').value=Math.max(start[1],p[1]);zone()}function up(){drag=0}function shot(){el('cam').src='/snapshot.jpg?t='+Date.now()}function init(){var v=document.querySelector('.view');v.onpointerdown=down;v.onpointermove=move;v.onpointerup=up;v.onpointerleave=up;document.querySelectorAll('input').forEach(function(x){x.oninput=zone});zone();setInterval(shot,3000)}"""

MOTION_REDIRECT = b"""<html><head><link rel=icon href=data:,><meta http-equiv=refresh content="0;URL=motiondect_other.asp?winid=0"></head></html>"""

SETTINGS_SHELL = b"""<html><head><link rel=icon href=data:,><style>html,body{margin:0;height:100%;font:14px Arial}header{height:42px;background:#345;color:white;display:flex;align-items:center;padding:0 12px}button{padding:6px 14px}iframe{border:0;width:100%;height:calc(100% - 42px)}</style></head><body><header><button onclick="location='main.asp'">&larr; Back</button></header><iframe id=f></iframe><script>var p=new URLSearchParams(location.search).get('p'),m={l:'mjpgmain.asp',i:'camera.asp',s:'stream.asp',n:'ethernet.asp',w:'wlan.asp',m:'motiondect_other.asp?winid=0',c:'otproxyc.asp',d:'storage.asp',y:'sysinfo.asp',e:'httpevent.asp',h:'alarmsend.asp',a:'account/acclist.asp'};f.src=m[p]||'sysinfo.asp'</script></body></html>"""

HA_PAGE = b"""<html><head><meta name=viewport content="width=device-width"><link rel=icon href=data:,><style>body{font:15px Arial;max-width:760px;margin:30px auto;line-height:1.5;color:#234}a.button{display:inline-block;padding:10px 16px;margin:5px;background:#345;color:white;text-decoration:none}code{background:#eee;padding:2px 5px}</style></head><body><p><a href=main.asp>&larr; Back</a></p><h1>Home Assistant / MQTT</h1><p><b>Optional integration.</b> The camera works without MQTT. The add-on publishes motion, refreshable snapshots and system telemetry, and proxies the MJPEG stream. No cloud is used.</p><ol><li>Add this camera to the Gigaset Camera MQTT add-on.</li><li>Copy its unique motion path from the add-on log.</li><li>Configure an HTTP server with the Home Assistant IP, port <code>8766</code>, Authorization <b>No</b>, then use that path for HTTP alarm delivery.</li></ol><p><a class=button href=motiondect2.asp?p=e>1. HTTP server</a><a class=button href=motiondect2.asp?p=h>2. Motion delivery</a><a class=button href=motiondect2.asp?p=m>3. Motion zones</a></p></body></html>"""

LIVE_PAGE = b"""<html><head><meta name=viewport content="width=device-width"><link rel=icon href=data:,><style>body{margin:0;background:#26343d;color:white;font:14px Arial}nav{padding:12px;background:#345}a{color:white;margin-right:18px}main{text-align:center;padding:12px}img{width:min(100%,854px);height:auto;background:#111}code{user-select:all}</style></head><body><nav><a href=main.asp>&larr; Back</a><a href=motiondect2.asp?p=s>Stream settings</a></nav><main><img src=../stream.jpg><p>Primary RTSP: <code id=p></code><br>Secondary RTSP: <code id=s></code></p></main><script>var b='rtsp://'+location.hostname+':554/';p.textContent=b+'live_h264.sdp';s.textContent=b+'live_h264_1.sdp'</script></body></html>"""

LIVE_REDIRECT = b"""<html><head><link rel=icon href=data:,><meta http-equiv=refresh content="0;URL=mjpgmain_hd.asp"></head></html>"""

INIT_SCRIPT = br'''#!/bin/sh
if [ "$1" = init ];then
 p=/var/etc/root.passwd;u=/var/etc/umconfig.txt;s=/var/etc/sys.conf;t=/var/tmp/u;v=/var/etc/.lm9;d=0
 if [ ! -f $v ];then
  cp /etc/passwd $p
  sed -e 's|psl_client=enable|psl_client=disable|' -e '/^\[otproxyc\]/,/^\[/s|ENABLE=enable|ENABLE=disable|' -e 's|SERVER=cam-dx.gigaset-elements.com|SERVER=|' $s >$t
  cat $t>$s;rm -f $t;touch $v;d=1
 fi
 [ -f $p ]||{ cp /etc/passwd $p;d=1;}
 r=$(sed -n '/^root_hash=/p' $u);[ -z "$r" ]||echo "${r#root_hash=}" >$p
 if [ -n "$(sed -n '\|name=/specset/|p' $u)" ];then sed 's|name=/specset/|name=/cgi-bin/|' $u >$t;cat $t>$u;rm -f $t;d=1;fi
 if [ -n "$(sed -n '/password=1234/p' $u)" ];then sed 's|password=1234|password=@ADMIN_PASSWORD@|' $u >$t;cat $t>$u;rm -f $t;d=1;fi
 [ "$d" = 1 ]&&sysconf save
 mkdir /var/etcrw;cp -a /etc/* /var/etcrw;cp $p /var/etcrw/passwd;mount -o bind /var/etcrw /etc
 cp -a /dev/* /var/dev;mount --bind /var/dev /dev;mknod /dev/ptmx c 5 2;mount -t devpts x /var/pts;/sbin/telnetd -p 23
 exit
fi
if [ "$1" = passwd ];then
 n=$2;t=/var/tmp/pw.in;(echo "$n";echo "$n")>$t
 /sbin/telnetd -p 2323 -l /bin/passwd;sleep 1
 /usr/bin/curl -s --max-time 8 -T $t telnet://127.0.0.1:2323 >/var/tmp/pw.log 2>&1
 r=$(sed -n '1p' /etc/passwd);u=/var/etc/umconfig.txt;sed '/^root_hash=/d' $u >$t;echo "root_hash=$r" >>$t;cat $t>$u;sysconf save;sync
 kill $(pidof telnetd) 2>/dev/null;/sbin/telnetd -p 23;rm -f $t
 exit
fi
'''

SECURITY_CGI = br'''#!/bin/sh
page(){ echo 'Content-Type: text/html';echo;echo "<html><body><p><a href=/en/main.asp>&larr; Back</a></p><h1>Security</h1><p>$1</p><h2>Web administrator</h2><a href=/en/motiondect2.asp?p=a>Manage admin password and users</a><h2>Root password</h2><form method=post><input type=password name=new required minlength=4 maxlength=8 pattern='[A-Za-z0-9._-]+' placeholder='New password'><input type=password name=confirm required minlength=4 maxlength=8 pattern='[A-Za-z0-9._-]+' placeholder='Repeat'><button>Change</button></form><h2>Factory reset</h2><p>Hold RESET to restore DHCP, root:root and the MAC-derived admin password.</p></body></html>";}
if [ "$REQUEST_METHOD" = POST ];then
 b=$(cat);n=${b#*new=};n=${n%%&*};c=${b#*confirm=};c=${c%%&*};l=${#n}
 case "$n" in *[!A-Za-z0-9._-]*|'') page 'Invalid password characters.';exit;;esac
 if [ "$n" != "$c" ] || [ "$l" -lt 4 ] || [ "$l" -gt 8 ];then page 'Passwords must match and contain 4-8 allowed characters.';exit;fi
 o=$(sed -n '1p' /etc/passwd)
 /home/web/specset/setdef.asp passwd "$n"
 if [ "$o" != "$(sed -n '1p' /etc/passwd)" ];then
  page 'Root password changed and saved.';exit
 fi
 page 'Password update failed.';exit
fi
page 'Use unique passwords. The web interface is HTTP-only, so keep the camera on a trusted local network.'
'''


def fit(content: bytes, size: int, label: str) -> bytes:
    if len(content) > size:
        raise ValueError(f"{label}: page is {len(content)} bytes, slot is only {size}")
    return content + b" " * (size - len(content))


def normalize_mac(mac: str) -> str:
    value = re.sub(r"[^0-9A-Fa-f]", "", mac).upper()
    if len(value) != 12:
        raise ValueError("MAC address must contain exactly 12 hexadecimal digits")
    return value


def default_admin_password(mac: str) -> bytes:
    value = normalize_mac(mac)
    material = "LUCKOTVF" + value[::-1] + "YCAMVF"
    return base64.b64encode(material.encode("ascii"))


def credential_summary(mac: str) -> str:
    value = normalize_mac(mac)
    formatted_mac = ":".join(value[index : index + 2] for index in range(0, 12, 2))
    admin_password = default_admin_password(value).decode("ascii")
    return (
        "\nCamera credentials\n"
        f"  MAC address:    {formatted_mac}\n"
        "  Web user:       admin\n"
        f"  Web password:   {admin_password}\n"
        "  Root user:      root\n"
        "  Root password:  root  (after first boot or factory reset)\n"
        "\nChange both passwords in the local web manager after installation."
    )


def make_default_umconfig(content: bytes) -> bytes:
    patched = content.replace(b"name=/specset/", b"name=/cgi-bin/", 1)
    if patched == content:
        raise ValueError("unexpected /etc/default/umconfig.txt layout")
    patched = patched.replace(b"\n\n", b"\n")
    return fit(patched, len(content), "/etc/default/umconfig.txt")


def make_rc_sysinit(content: bytes, mac: str) -> bytes:
    text = content.decode("ascii")
    lines = [
        line if line.startswith("#!") else line.strip()
        for line in text.splitlines()
        if line.startswith("#!") or (line.strip() and not line.lstrip().startswith("#"))
    ]
    compact = "\n".join(lines) + "\n"
    marker = "sysconf load\n"
    block = "/home/web/specset/setdef.asp init\n"
    if marker not in compact:
        raise ValueError("sysconf load missing from rc.sysinit")
    return fit((compact.replace(marker, marker + block)).encode("ascii"), len(content), "/etc/init.d/rc.sysinit")


def make_default_root_passwd(content: bytes) -> bytes:
    lines = content.splitlines(keepends=True)
    root_indexes = [index for index, line in enumerate(lines) if line.startswith(b"root:")]
    if root_indexes != [0]:
        raise ValueError("/etc/passwd: expected root as the first and only root entry")
    ending = b"\n" if lines[0].endswith(b"\n") else b""
    lines[0] = (
        b"root:$1$Sl83jCfU$VdXfR.95vgh7WCkRWHXUL1:0:0:root:/root:/bin/sh"
        + ending
    )
    result = b"".join(lines)
    if len(result) != len(content):
        raise ValueError("/etc/passwd: replacement changed the file length")
    return result


def find_inode_position(image: bytearray, filesystem, node, path: str) -> int:
    word0 = node.mode | (node.uid << 16)
    word1 = node.size | (node.gid << 24)
    signature = struct.pack("<II", word0, word1)
    basename = path.rsplit("/", 1)[-1].encode("ascii")
    candidates = []
    position = filesystem.base
    while True:
        position = image.find(signature, position, filesystem.limit)
        if position < 0:
            break
        if image[position + 16 : position + 16 + len(basename)] == basename:
            candidates.append(position)
        position += 1
    if len(candidates) != 1:
        raise ValueError(f"{path}: expected one inode, found {len(candidates)}")
    return candidates[0]


def make_inode_owner_executable(image: bytearray, filesystem, node, path: str) -> None:
    position = find_inode_position(image, filesystem, node, path)
    word0 = struct.unpack_from("<I", image, position)[0]
    struct.pack_into("<I", image, position, word0 | 0o100)
    print(f"{path}: owner execute bit enabled")


def swap_directory_contents(image: bytearray, filesystem, nodes, first: str, second: str) -> None:
    first_pos = find_inode_position(image, filesystem, nodes[first], first)
    second_pos = find_inode_position(image, filesystem, nodes[second], second)
    first_w1, first_w2 = struct.unpack_from("<II", image, first_pos + 4)
    second_w1, second_w2 = struct.unpack_from("<II", image, second_pos + 4)
    first_new_w1 = (first_w1 & 0xFF000000) | (second_w1 & 0x00FFFFFF)
    second_new_w1 = (second_w1 & 0xFF000000) | (first_w1 & 0x00FFFFFF)
    first_new_w2 = (second_w2 & ~0x3F) | (first_w2 & 0x3F)
    second_new_w2 = (first_w2 & ~0x3F) | (second_w2 & 0x3F)
    struct.pack_into("<II", image, first_pos + 4, first_new_w1, first_new_w2)
    struct.pack_into("<II", image, second_pos + 4, second_new_w1, second_new_w2)
    print(f"{first} <-> {second}: directory contents swapped")


def make_cloudless_sysconf(content: bytes) -> bytes:
    # Keep automatic service management so the web Enable/Disable switch is
    # persistent.  The service itself is disabled below by default, while
    # procspy never reboots the camera merely because that client is absent.
    patched = content
    patched = patched.replace(
        b"HOSTNAME=www.gigaset.com",
        b"HOSTNAME=" + b" " * len(b"www.gigaset.com"),
        1,
    )
    patched = patched.replace(
        b"psl_client=enable\n\n[specfunc]",
        b"psl_client=disable\n[specfunc]",
        1,
    )
    old_section = (
        b"[otproxyc]\nFUNC=pslclient\nENABLE=enable\n"
        b'USERAGENT="IPCAM 1.0.1"\nSERVER=cam-dx.gigaset-elements.com\n'
        b"PORT=8000\nSTATUSFILE=/var/tmp/otproxyc_status\n\n[otu]"
    )
    new_section = old_section.replace(b"ENABLE=enable", b"ENABLE=disable")
    new_section = new_section.replace(
        b"cam-dx.gigaset-elements.com",
        b" " * len(b"cam-dx.gigaset-elements.com"),
    )
    new_section = new_section.replace(b"\n\n[otu]", b"\n[otu]")
    if len(old_section) != len(new_section):
        raise AssertionError("cloudless section must retain its original size")
    patched = patched.replace(old_section, new_section, 1)
    if patched == content or len(patched) != len(content):
        raise ValueError("unexpected /etc/default/sys.conf layout")
    return patched


def patch_mef(source: Path, destination: Path, mac: str) -> None:
    image = bytearray(source.read_bytes())
    if image[:4] != b"MEF\x7f":
        raise ValueError(f"{source}: not a MEF image")

    filesystem = cramfs_extract.Cramfs(bytes(image))
    nodes = filesystem.walk()
    init_script = INIT_SCRIPT.replace(b"@ADMIN_PASSWORD@", default_admin_password(mac))
    security_cgi = SECURITY_CGI
    targets = {
        "/home/web/default.asp": DEFAULT_PAGE,
        "/home/web/en/main.asp": DASHBOARD,
        "/home/web/en/motiondect2.asp": SETTINGS_SHELL,
        "/home/web/en/motiondect.asp": MOTION_REDIRECT,
        "/home/web/en/motiondect_other.asp": MOTION_PAGE,
        "/home/web/en/motiondect.htm": HA_PAGE,
        "/home/web/en/mjpgmain.asp": LIVE_PAGE,
        "/home/web/en/mjpgmain_hd.asp": LIVE_PAGE,
        "/home/web/en/quicktime_player.asp": LIVE_REDIRECT,
        "/home/web/en/quicktime_player_hd.asp": LIVE_REDIRECT,
        "/home/web/en/quicktime_player2.asp": LIVE_REDIRECT,
        "/home/web/en/quicktime_player2_hd.asp": LIVE_REDIRECT,
        "/home/web/cab/swfobject.js": MOTION_JS,
        "/home/web/debug825/video.asp": security_cgi,
        "/home/web/specset/setdef.asp": init_script,
    }
    for path, replacement in targets.items():
        node = nodes.get(path)
        if node is None:
            raise ValueError(f"{path}: not present in firmware")
        original = filesystem.read_file(node)
        patch_file_many(image, filesystem, path, [(original, fit(replacement, len(original), path))])

    special_targets = {
        "/etc/init.d/rc.sysinit": lambda content: make_rc_sysinit(content, mac),
        "/etc/passwd": make_default_root_passwd,
    }
    for path, transform in special_targets.items():
        node = nodes.get(path)
        if node is None:
            raise ValueError(f"{path}: not present in firmware")
        original = filesystem.read_file(node)
        patch_file_many(image, filesystem, path, [(original, transform(original))])

    cgi_source = "/home/web/debug825/video.asp"
    make_inode_owner_executable(image, filesystem, nodes[cgi_source], cgi_source)
    init_source = "/home/web/specset/setdef.asp"
    make_inode_owner_executable(image, filesystem, nodes[init_source], init_source)

    # Cramfs directory names must remain sorted. Give the stock empty CGI
    # directory the obsolete debug825 contents, and leave debug825 empty.
    swap_directory_contents(
        image, filesystem, nodes, "/home/web/cgi-bin", "/home/web/debug825"
    )

    sysconf_path = "/etc/default/sys.conf"
    sysconf_node = nodes.get(sysconf_path)
    if sysconf_node is None:
        raise ValueError(f"{sysconf_path}: not present in firmware")
    original_sysconf = filesystem.read_file(sysconf_node)
    patch_file_many(
        image,
        filesystem,
        sysconf_path,
        [(original_sysconf, make_cloudless_sysconf(original_sysconf))],
    )

    # Standard cramfs fsid CRC32 covers the complete filesystem image with its
    # own CRC field zeroed.  The stock web updater validates this before MEF.
    cramfs_crc_offset = filesystem.base + 32
    image[cramfs_crc_offset : cramfs_crc_offset + 4] = b"\0\0\0\0"
    cramfs_checksum = zlib.crc32(
        image[filesystem.base : filesystem.base + filesystem.fs_size]
    ) & 0xFFFFFFFF
    struct.pack_into("<I", image, cramfs_crc_offset, cramfs_checksum)

    # MEF stores its CRC32 at offset 0x3c and calculates it with that field zeroed.
    image[0x3C:0x40] = b"\0\0\0\0"
    checksum = zlib.crc32(image) & 0xFFFFFFFF
    struct.pack_into("<I", image, 0x3C, checksum)
    destination.write_bytes(image)
    print(
        f"wrote {destination} ({len(image)} bytes), "
        f"cramfs=0x{filesystem.base:X}, cramfs_crc32=0x{cramfs_checksum:08X}, "
        f"mef_crc32=0x{checksum:08X}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="source MEF read from your camera")
    parser.add_argument("output", type=Path, help="patched MEF to install")
    parser.add_argument("--mac", required=True, help="camera MAC used for its factory web password")
    args = parser.parse_args()
    summary = credential_summary(args.mac)
    patch_mef(args.input, args.output, args.mac)
    print(summary)


if __name__ == "__main__":
    main()
