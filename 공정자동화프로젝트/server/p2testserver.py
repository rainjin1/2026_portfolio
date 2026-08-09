from pymodbus.client import ModbusTcpClient

client = ModbusTcpClient("192.168.3.40", port=4001)

if client.connect():
    print("연결 성공")

    # FC06: 레지스터 0번에 123 쓰기
    result = client.write_register(0, 123)
    print(f"FC06 결과: {result}")

    # FC03: 레지스터 0번부터 5개 읽기
    result = client.read_holding_registers(0, 5)
    if not result.isError():
        print(f"FC03 결과: {result.registers}")
    else:
        print(f"FC03 에러: {result}")

    client.close()
else:
    print("연결 실패")