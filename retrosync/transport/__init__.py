"""Low-level device transports.

Things in here speak a wire protocol to a hardware device. They are
intentionally separated from `sources/` because the same transport
can back multiple source adapters (e.g. one Krikzz FT245 module
serves EverDrive 64 X7, Mega EverDrive Pro, and other Krikzz
flash carts that share a USB protocol).
"""
