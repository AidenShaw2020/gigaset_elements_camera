"""Run the verified S2Lm DDR ADS through native libusb-win32/libusb0."""

import usb.backend.libusb0

import s2lm_ddr_init as tool


LIBUSB0_DLL = r"C:\Windows\System32\libusb0.dll"


def libusb0_backend():
    backend = usb.backend.libusb0.get_backend(
        find_library=lambda _name: LIBUSB0_DLL
    )
    if backend is None:
        raise RuntimeError("Could not load %s" % LIBUSB0_DLL)
    return backend


tool.SOC_PROFILE = "S2Lm / native libusb-win32"
tool.get_backend = libusb0_backend


if __name__ == "__main__":
    tool.main()
