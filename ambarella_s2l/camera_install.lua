-- Source-only one-shot installer for the Ambarella S2L local gateway.
-- This runs as root through the stock ycam_autorun.sh service mechanism.

local root = os.getenv("GIGASET_INSTALL_ROOT") or ""
local service_dir = os.getenv("GIGASET_SERVICE_DIR")
    or "/mnt/mass_storage_folder/gigaset_local_gateway"

local function target(path)
    return root .. path
end

local function read_all(path)
    local file, message = io.open(path, "rb")
    if not file then
        error("cannot read " .. path .. ": " .. tostring(message))
    end
    local data = file:read("*a")
    file:close()
    return data
end

local function write_all(path, data)
    local temporary = path .. ".gigaset-new"
    local file, message = io.open(temporary, "wb")
    if not file then
        error("cannot write " .. temporary .. ": " .. tostring(message))
    end
    assert(file:write(data))
    assert(file:close())
    if read_all(temporary) ~= data then
        os.remove(temporary)
        error("read-back verification failed for " .. path)
    end
    if not os.rename(temporary, path) then
        os.remove(temporary)
        error("cannot activate " .. path)
    end
end

local function file_exists(path)
    local file = io.open(path, "rb")
    if file then
        file:close()
        return true
    end
    return false
end

local function backup_once(path)
    local backup = path .. ".gigaset-stock"
    if not file_exists(backup) then
        write_all(backup, read_all(path))
    end
end

local function replace_once(text, old, replacement, description)
    local first_start, first_end = string.find(text, old, 1, true)
    if not first_start then
        error("unsupported firmware: missing " .. description .. " anchor")
    end
    if string.find(text, old, first_end + 1, true) then
        error("unsupported firmware: duplicate " .. description .. " anchor")
    end
    return string.sub(text, 1, first_start - 1)
        .. replacement
        .. string.sub(text, first_end + 1)
end

local cec_path = target("/usr/local/bin/cec_init.sh")
local lighttpd_path = target("/etc/lighttpd/lighttpd.conf")

local cec = read_all(cec_path)
if not string.find(cec, "# gigaset-elements-camera local gateway", 1, true) then
    cec = replace_once(
        cec,
        "\tfi\n\n\tif [ -e /dev/adc/OTA_upgrade ]\n",
        "\tfi\n\n\t# gigaset-elements-camera local gateway\n"
            .. "\t/usr/local/bin/gigaset_local_gateway.sh monitor &\n\n"
            .. "\tif [ -e /dev/adc/OTA_upgrade ]\n",
        "cec_init"
    )
end

local lighttpd = read_all(lighttpd_path)
if not string.find(lighttpd, "cgi-bin/wifi_setup\\.cgi", 1, true) then
    lighttpd = replace_once(
        lighttpd,
        'url.rewrite-once = ( "[Ff]+[Oo]+[Rr]+[Mm]+/(.*)" => "/Form/$1" )',
        'url.rewrite-once = (\n'
            .. '  "[Ff]+[Oo]+[Rr]+[Mm]+/(.*)" => "/Form/$1",\n'
            .. '  "^/(generate_204|gen_204|hotspot-detect.html|connecttest.txt|ncsi.txt)$" => "/setup/index.html",\n'
            .. '  "^/library/test/success.html$" => "/setup/index.html"\n'
            .. ')',
        "URL rewrite"
    )
    lighttpd = replace_once(
        lighttpd,
        '#cgi.assign = (".cgi" => "",".py" => "/usr/bin/python")',
        'cgi.assign = (".cgi" => "")',
        "CGI assignment"
    )
    lighttpd = replace_once(
        lighttpd,
        '$HTTP["url"] =~ "/" {',
        '$HTTP["url"] !~ "^/(setup/|cgi-bin/wifi_setup\\.cgi|generate_204|gen_204|hotspot-detect\\.html|library/test/success\\.html|connecttest\\.txt|ncsi\\.txt)" {',
        "authentication"
    )
end

-- Validate every source and every stock anchor before changing the rootfs.
local gateway = read_all(service_dir .. "/gigaset_local_gateway.sh")
local cgi = read_all(service_dir .. "/wifi_setup.cgi")
local page = read_all(service_dir .. "/index.html")
if #gateway == 0 or #cgi == 0 or #page == 0 then
    error("installer payload is incomplete")
end

backup_once(cec_path)
backup_once(lighttpd_path)

write_all(target("/usr/local/bin/gigaset_local_gateway.sh"), gateway)
write_all(target("/webSvr/web/cgi-bin/wifi_setup.cgi"), cgi)
write_all(target("/webSvr/web/setup/index.html"), page)
write_all(cec_path, cec)
write_all(lighttpd_path, lighttpd)

print("Local gateway files installed and verified.")
