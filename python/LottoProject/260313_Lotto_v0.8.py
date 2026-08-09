import random
 
list_Lottoballs = [balls for balls in range(1, 46, 1)] # 1부터 46전까지(45까지) 리스트생성
Dic_FinalLottocount = {} # 나온 결과를 저장할 딕셔너리. 이 딕셔너리가 결과를 전부 가지고있을 예정. 그래프 만드는데도 사용.
for i in range (1, 46, 1):
    Dic_FinalLottocount[i] = 0 # 딕셔너리 생성 1: 0, 2:0 이런식으로 생성. 1부터 46전까지(45까지)
Dic_Lottocount = {}
for i in range(1, 1001): #1000번반복
    Dic_Lottocount[i] = [] #로또 결과 천번을 저장할 딕셔너리.
for number in range (1, 1001, 1): #천번반복
    random.shuffle(list_Lottoballs) #셔플
    for num in range(1, 6):
        Dic_Lottocount[number].append(list_Lottoballs[0]) #처음나온공을 로또카운트딕셔너리의 number키에 추가
        Dic_FinalLottocount[list_Lottoballs[0]] +=1 #파이널 로또카운트, 1~45 중 나온 값의 키에 횟수 1추가
        del list_Lottoballs[0] #뽑았으니까 공 삭제
    list_Lottoballs = [balls for balls in range(1, 46, 1)] #공을 5개 삭제했으니, 다시 리스트를 선언해서 초기화
list_counter = list(Dic_FinalLottocount.items())
#5개씩 뽑힌 천개의 리스트를 보고싶다면
#print(Dic_Lottocount)

#많이 뽑힌 숫자 순서대로 정렬
#print(sorted(Dic_FinalLottocount.items(), key=lambda x: x[1], reverse = True))

Top5Nums = sorted(Dic_FinalLottocount.items(), key=lambda x: x[1], reverse = True)[:5] #그중 탑 5개만 가져오기 // 중복이 있을시 뭘 가져오는지는 아직 계산되지 않음.
Congratulations = [item[0] for item in Top5Nums]
# 최종 결과를 출력하려면
print(Congratulations)
Graph_Dic_FinalLottocount = {}
for key, value in Dic_FinalLottocount.items():
    Graph_Dic_FinalLottocount[key] = "*" * (value//10)
for key, value in Graph_Dic_FinalLottocount.items():
    print(f"{key}: {value}", end="\n")
