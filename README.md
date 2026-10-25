# Furnace

Furnace is a standalone blocklist server for a Palo Alto firewall.

SOC (Security Operations Center) analysts click a button in Cortex XSOAR.
XSOAR calls the Furnace API with an IP address or a domain.
Furnace saves it and publishes it as an EDL (External Dynamic List).
An EDL is a plain text file. The firewall downloads it again and again and blocks what is in it.

Furnace keeps working when XSOAR is down. Only the button stops working.

The full guide is being written. See the steps below as they are finished.
