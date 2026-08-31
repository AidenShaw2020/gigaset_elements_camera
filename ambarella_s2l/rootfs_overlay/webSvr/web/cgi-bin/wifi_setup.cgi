#!/usr/bin/lua

local function respond(status, title, message)
    io.write("Status: " .. status .. "\r\n")
    io.write("Content-Type: text/html; charset=utf-8\r\n")
    io.write("Cache-Control: no-store\r\n\r\n")
    io.write("<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'>")
    io.write("<title>" .. title .. "</title><style>body{font:16px system-ui;max-width:34rem;margin:10vh auto;padding:2rem}</style>")
    io.write("<h1>" .. title .. "</h1><p>" .. message .. "</p>")
end

local function url_decode(value)
    value = value:gsub("%+", " ")
    return value:gsub("%%(%x%x)", function(hex)
        return string.char(tonumber(hex, 16))
    end)
end

local function parse_form(body)
    local values = {}
    for pair in body:gmatch("[^&]+") do
        local key, value = pair:match("^([^=]*)=(.*)$")
        if key then
            values[url_decode(key)] = url_decode(value)
        end
    end
    return values
end

local function valid_text(value)
    return not value:find("%c") and not value:find("%z")
end

local function quote_wpa(value)
    value = value:gsub("\\", "\\\\")
    value = value:gsub('"', '\\"')
    return '"' .. value .. '"'
end

local state = io.open("/var/run/gigaset-local-gateway.state", "r")
local state_value = state and state:read("*l") or ""
if state then
    state:close()
end
if not state_value:match("^setup%-ap:") then
    respond("403 Forbidden", "Setup is inactive", "Wi-Fi settings can be changed only while connected to the camera's temporary setup network.")
    os.exit(0)
end

if os.getenv("REQUEST_METHOD") ~= "POST" then
    respond("405 Method Not Allowed", "Method not allowed", "Open <a href='/setup/'>the setup page</a> and submit the form.")
    os.exit(0)
end

local length = tonumber(os.getenv("CONTENT_LENGTH") or "0") or 0
if length < 1 or length > 4096 then
    respond("400 Bad Request", "Invalid request", "The submitted form has an invalid size.")
    os.exit(0)
end

local form = parse_form(io.read(length) or "")
local ssid = form.ssid or ""
local password = form.password or ""

if #ssid < 1 or #ssid > 32 or not valid_text(ssid) then
    respond("400 Bad Request", "Invalid Wi-Fi name", "SSID must contain 1 to 32 bytes and no control characters.")
    os.exit(0)
end
if (#password > 0 and #password < 8) or #password > 63 or not valid_text(password) then
    respond("400 Bad Request", "Invalid password", "Use 8 to 63 characters, or leave it empty for an open network.")
    os.exit(0)
end

os.execute("/bin/mkdir -p /var/wifi")
local temporary = "/var/wifi/wifi.conf.new"
local config = io.open(temporary, "w")
if not config then
    respond("500 Internal Server Error", "Save failed", "The camera could not write its Wi-Fi configuration.")
    os.exit(0)
end

config:write("ctrl_interface=/var/run/wpa_supplicant\n")
config:write("update_config=0\n")
config:write("network={\n")
config:write("    ssid=" .. quote_wpa(ssid) .. "\n")
config:write("    scan_ssid=1\n")
if #password == 0 then
    config:write("    key_mgmt=NONE\n")
else
    config:write("    key_mgmt=WPA-PSK\n")
    config:write("    psk=" .. quote_wpa(password) .. "\n")
end
config:write("}\n")
config:close()

os.execute("/bin/chmod 600 " .. temporary)
if not os.rename(temporary, "/var/wifi/wifi.conf") then
    os.remove(temporary)
    respond("500 Internal Server Error", "Save failed", "The camera could not activate its Wi-Fi configuration.")
    os.exit(0)
end
os.execute("/bin/sync")

respond("200 OK", "Configuration saved", "The setup network will now close while the camera connects. This can take about 30 seconds. If it fails, the setup network will return.")
os.execute("(/bin/sleep 2; /usr/local/bin/gigaset_local_gateway.sh apply >/dev/null 2>&1) &")
