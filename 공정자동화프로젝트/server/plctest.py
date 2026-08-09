import pymcprotocol

plc = pymcprotocol.Type3E()
plc.connect("192.168.3.39", 3900)

result = plc.batchread_bitunits(headdevice="M9212", readsize=1)
print("D100:", result[0])

plc.close()