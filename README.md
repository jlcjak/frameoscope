# frameoscope
oscilloscope expansion card / module for framework laptops 

## Tech Specs
- sampling rate: 40MSPS
- bandwidth: 10MHz
- resolution: 8bit
- interface: usb2
- front end Rin: 8.5KOhm
- front end Cin: ~5pF
- input voltage: 0-5V (protected from reverse and high voltage)

## Remarks
- The board is mainly comprimized of a TI adc, an iCE40 fpga and a usb PHY (FT232H).
- The fpga is used as an protocol tranlator between the busses on the usb PHY and adc.
- You can program the fpga directly over usb, through FT232H.
- There is no flash on the fpga so you need to reprogram it on reset or use iCE nvcm

## manufacturing
- all components are sourcable from lcsc, for easy assembly in china

![Alt text](frameoscope_schematic.png)
![Alt text](frameoscope_render.png)
