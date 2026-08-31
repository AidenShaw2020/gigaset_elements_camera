#!/bin/sh

# Local Wi-Fi recovery for the Ambarella S2L Gigaset camera.
# The normal station connection is left untouched when it succeeds.  If it
# does not succeed within 30 seconds, the stock Wi-Fi stack is switched to an
# open, temporary setup access point with a local captive page.

PATH=/sbin:/bin:/usr/sbin:/usr/bin:/usr/local/bin
WAIT_SECONDS=30
LOCK_DIR=/var/run/gigaset-local-gateway.lock
STATE_FILE=/var/run/gigaset-local-gateway.state
STA_IF=wlan0
AP_IF=wlan0
WIFI_CONFIG=/var/wifi/wifi.conf
SETUP_IP=192.168.42.1
AP_LOG=/var/log/gigaset-setup-ap.log
PERSISTENT_LOG=/dev/adc/gigaset-local-gateway.log

log()
{
        message="gigaset-local-gateway: $*"
        /bin/echo "$message" > /dev/console
        /bin/echo "$message" >> /var/log/gigaset-local-gateway.log
        [ -d /dev/adc ] && /bin/echo "$message" >> "$PERSISTENT_LOG"
}

log_ap_diagnostics()
{
        {
                /bin/echo "--- AP diagnostics ---"
                /bin/echo -n "op_mode="
                /bin/cat /sys/module/bcmdhd/parameters/op_mode 2>/dev/null || true
                /sbin/ifconfig ${AP_IF} 2>&1 || true
                /bin/cat "$AP_LOG" 2>/dev/null || true
        } >> /var/log/gigaset-local-gateway.log
        if [ -d /dev/adc ]
        then
                /bin/cat /var/log/gigaset-local-gateway.log >> "$PERSISTENT_LOG"
        fi
}

stop_process()
{
        /usr/bin/killall "$1" >/dev/null 2>&1 || true
}

station_ready()
{
        state=""
        for control_dir in /var/run/wpa_supplicant /var/wifi/wpa_supplicant
        do
                state=`/usr/sbin/wpa_cli -i${STA_IF} -p${control_dir} status 2>/dev/null | /bin/grep '^wpa_state='`
                [ "$state" = "wpa_state=COMPLETED" ] && break
        done
        [ "$state" = "wpa_state=COMPLETED" ] || return 1
        /sbin/ifconfig ${STA_IF} 2>/dev/null | /bin/grep -Eq 'inet addr:|inet '
}

wait_for_station()
{
        remaining=$WAIT_SECONDS
        while [ "$remaining" -gt 0 ]
        do
                station_ready && return 0
                /bin/sleep 1
                remaining=$((remaining - 1))
        done
        return 1
}

reload_wifi_driver()
{
        wanted_mode=$1
        tries=0

        while [ "$tries" -lt 10 ]
        do
                current_mode=`/bin/cat /sys/module/bcmdhd/parameters/op_mode 2>/dev/null`
                [ "$current_mode" = "$wanted_mode" ] && return 0
                /sbin/rmmod bcmdhd >/dev/null 2>&1 || true
                /bin/sleep 1
                /sbin/modprobe bcmdhd iface_name=wlan0 dhd_msg_level=0x00 op_mode=${wanted_mode} || true
                /bin/sleep 2
                tries=$((tries + 1))
        done
        return 1
}

camera_suffix()
{
        mac=`/bin/cat /sys/class/net/${STA_IF}/address 2>/dev/null | /usr/bin/tr -d ':' | /usr/bin/tr 'a-f' 'A-F'`
        suffix=`/bin/echo "$mac" | /usr/bin/tail -c 7`
        [ -n "$suffix" ] || suffix=SETUP
        /bin/echo "$suffix"
}

start_setup_ap()
{
        suffix=`camera_suffix`
        ssid="Gigaset-C-${suffix}"

        stop_process udhcpc
        stop_process wpa_supplicant
        stop_process dnsmasq

        # Use the camera vendor's own factory-mode path.  Besides selecting
        # wlan0 it performs the exact bcmdhd unload/reload and Bluetooth reset
        # sequence expected by this board.
        /usr/local/bin/wifi_switch.sh ap "$ssid" 6 >"$AP_LOG" 2>&1 || {
                log "factory AP setup failed"
                log_ap_diagnostics
                return 1
        }

        /bin/sleep 2
        stop_process udhcpd
        /sbin/ifconfig ${AP_IF} ${SETUP_IP} netmask 255.255.255.0 up || {
                log "cannot assign setup IP to ${AP_IF}"
                log_ap_diagnostics
                return 1
        }

        stop_process dnsmasq
        /usr/sbin/dnsmasq \
                --interface=${AP_IF} \
                --bind-interfaces \
                --dhcp-range=192.168.42.2,192.168.42.20,255.255.255.0,12h \
                --address=/#/${SETUP_IP} || {
                log "captive DNS/DHCP startup failed"
                log_ap_diagnostics
                return 1
        }

        /bin/echo "setup-ap:${ssid}" > "$STATE_FILE"
        log "setup access point ${ssid} is ready at http://${SETUP_IP}/setup/"
        log_ap_diagnostics
}

start_station()
{
        stop_process dnsmasq
        stop_process wpa_supplicant
        stop_process udhcpc

        reload_wifi_driver 1 || return 1
        /sbin/ifconfig ${STA_IF} up || return 1
        /usr/sbin/wpa_supplicant -Dnl80211 -i${STA_IF} -c${WIFI_CONFIG} -B || return 1
        /bin/sleep 2
        /sbin/udhcpc -i${STA_IF} >/var/log/gigaset-udhcpc.log 2>&1 &
        wait_for_station
}

monitor()
{
        [ -d /dev/adc ] && : > "$PERSISTENT_LOG"
        : > /var/log/gigaset-local-gateway.log
        log "monitor started"
        wait_for_station && {
                /bin/echo station > "$STATE_FILE"
                log "station connection is ready"
                exit 0
        }

        log "station timeout; starting factory AP"
        /bin/mkdir "$LOCK_DIR" 2>/dev/null || exit 0
        start_setup_ap
        /bin/rmdir "$LOCK_DIR" 2>/dev/null || true
}

apply_config()
{
        /bin/mkdir "$LOCK_DIR" 2>/dev/null || exit 1
        /bin/sleep 2

        if start_station
        then
                /bin/echo station > "$STATE_FILE"
                log "new Wi-Fi configuration connected"
        else
                log "new Wi-Fi configuration failed; restoring setup access point"
                start_setup_ap
        fi

        /bin/rmdir "$LOCK_DIR" 2>/dev/null || true
}

case "$1" in
        monitor)
                monitor
                ;;
        apply)
                apply_config
                ;;
        ap)
                start_setup_ap
                ;;
        *)
                /bin/echo "Usage: $0 {monitor|apply|ap}" >&2
                exit 2
                ;;
esac
