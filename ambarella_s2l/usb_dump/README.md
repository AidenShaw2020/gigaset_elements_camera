# Ambarella S2Lm USB NAND research tools

> [!CAUTION]
> Only the restricted RAM-loader **read-only dump path** described below has
> been validated. NAND writers and boot-repair scripts in this directory are
> unfinished development artifacts. A test camera currently has a damaged
> BST/BLD boot chain after an incompatible generic partition layout was used.
> Do not run those writers on another camera.

These source-only tools reproduce the hardware-validated 128 MiB logical NAND
read for camera S30851-H2531-R101. They do not contain Ambarella binaries. The
user must supply compatible `platform.zip` and `Ambarella.zip` archives; the
scripts reject files whose required ADS and BLD members do not match the
expected SHA-256 values.

Hardware photographs and the current recovery state are documented in the
[S30851-H2531-R101 research notes](../../docs/cameras/s30851-h2531-r101.md).

## Tool classification

Validated read-only workflow:

- `s2lm_ddr_init_libusb0_v11.py` — volatile DDR initialization;
- `s2lm_libusb0_rawnand_bld_v14.py` — restricted RAM BLD launcher;
- `s2lm_libusb0_fulldump_v14.py` — CRC-checked logical NAND reader.

Diagnostic/readback helpers such as `s2lm_libusb0_damage_scan_v19.py` and
`s2lm_libusb0_rootfs_verify_v18.py` do not provide an installation path.

The `partwriter`, `rootfs_writer`, `rootfs_raw_writer` and `boot_repair`
scripts are **not release tools**. Their apparent success status was not a
durability guarantee, and the recovery unit's NAND did not match expected
readback. They are retained only to make the failed experiment auditable while
the recovery method is being corrected.

## Safety properties

- DDR initialization writes only volatile SoC/DDR state.
- The BLD is copied to RAM and is never installed in NAND.
- Before execution, command handlers 0, 1 and 4 are redirected to the BLD's
  own unknown-command handler.
- Only GET (2) and SEND (3) remain reachable.
- GET subtype 1 is patched in RAM to call the verified NAND-read function.
- Every chunk is checked against the device CRC32 and recorded with SHA-256.
- A power cycle removes every RAM patch.

## Windows prerequisites

Install the Python requirements from the repository root. The proven transport
uses the 64-bit `libusb-win32` driver and `C:\Windows\System32\libusb0.dll` for
both USB devices:

- BootROM `4255:000A`
- RAM BLD `4255:0001`

Driver binding is per PID. Installing it only for `000A` is not sufficient.

The camera entered USB BootROM when the marked service pad below the micro-USB
area was pulled to GND through 4.7 kOhm at power-on while RESET was held.
RESET was released after approximately 2–3 seconds. Keep the 4.7 kOhm boot
strap connected through the BLD jump and release it only after `4255:0001`
appears. Entry proved sensitive to contact and timing; this is a research
procedure, not a finished end-user workflow.

## Procedure

Run from this directory. First initialize DDR while `4255:000A` is present:

```powershell
python .\s2lm_ddr_init_libusb0_v11.py C:\path\to\platform.zip --apply
```

Upload, verify and execute the command-restricted RAM BLD:

```powershell
python .\s2lm_libusb0_rawnand_bld_v14.py launch C:\path\to\Ambarella.zip
```

Type `LAUNCH`, then keep RESET released and type `RUN`. After `4255:0001`
enumerates, release the 4.7 kOhm strap and make the dump:

```powershell
python .\s2lm_libusb0_fulldump_v14.py camera-read-1.bin
```

If USB is interrupted, repeat BootROM entry, DDR initialization and BLD launch,
then continue the existing `.part` file:

```powershell
python .\s2lm_libusb0_fulldump_v14.py camera-read-1.bin --resume
```

Cold-start the process and make a second independent read. Keep both files only
if they are exactly 134,217,728 bytes and their SHA-256 values match:

```powershell
Get-FileHash -Algorithm SHA256 .\camera-read-1.bin, .\camera-read-2.bin
```

Dump files, manifests containing device results, Ambarella archives and
extracted filesystems are private recovery material and must not be committed.
