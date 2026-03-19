import cv2
import numpy as np

# 1. 이미지 불러오기
img = cv2.imread("sample_img1.jpg")
if img is None:
    print("이미지를 불러올 수 없습니다.")
    exit()

output = img.copy()

# 2. 전처리 인식할 수 있도록 흑백(그레이)로 처리 및 가우시안 블러를 이용한 노이즈 및 윤곽선 처리.
gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
blur = cv2.GaussianBlur(gray, (5, 5), 0)

# 3-1. edge : 선분이 직진하다가, 희게 변하는 부분을 꼭짓점으로 인식 할 수 있게 정보제공 = 테두리만 뽑아낸 밑그림
# 3-2. kernel : 조명이나 화질 등 여러 변수로 인해 도형이 끊어져서 인식될 수 있음. 그 윤곽선을 분석해서 이어주는 처리
#               = 혹시나 테두리가 끊겨있다거나 하면 그걸 이어주는 덧칠용 3*3짜리 도장.
# 3-3. edges : 3-1에서 받은 정보(edge에서 변화값(50이하는 버리고, 150이상이면 선분의 끝으로 인식)),
#              3-2에서 이어준 윤곽선을 토대로 선을 확실하게 그려주기
#              =확실하게 테두리가 완성됨.
edges = cv2.Canny(blur, 50, 150)
kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
edges = cv2.dilate(edges, kernel)

# 4. 윤곽선 찾아서 저장하기
#                             1번변수      2번변수              3번변수
contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
# 1번변수 edges : 3에서 계산된 edges(완성된 테두리)를 이용하고
# 2번변수 cv2.RETR_EXTERNAL : 가장 바깥쪽 라인만 인식하기
# 3번변수 cv2.CHAIN_APPROX_SIMPLE : 선은 생략하고, 선이 꺽이는 꼭짓점만 저장하기. 추후에 도형을 구분할때 사용.


print(f"{'No.':<5} | {'Shape':<10} | {'Center (x,y)':<15} | {'Angle (Deg)':<10}")
print("-" * 55)

count = 1
#4번에서 저장된 윤곽선들의 리스트를 사용
for cnt in contours:
    #픽셀 면적 계산함수
    area = cv2.contourArea(cnt)

    # 노이즈 제거 300이하의 도형은 인식하지않음.
    if area < 300:
        continue

    # 중심 좌표 계산
    # cv2.moments는 완성된 테두리의 좌표 리스트를 가지고 면적 및 무게중심(도형의 중심점)을 계산
    M = cv2.moments(cnt)
    if M["m00"] == 0: continue
    # m10 : x좌표값들의 합
    # m01 : y좌표값들의 합
    # m00 : 전체 점의 갯수. *꼭짓점뿐만이 아닌, 도형을 구성하는 모든 좌표의 합산. 면적 이라고 보면 됨.
    cx = int(M["m10"] / M["m00"])
    cy = int(M["m01"] / M["m00"])

    # 각도계산
    # cnt를 받아서 전체 윤곽선을 토대로 얼마나 기울어져있는지를 자동으로 계산해줌.
    rect = cv2.minAreaRect(cnt)
    #cv2.minAreaRect(cnt)의 결과물이 아래 (x, y), (w, h), angle = rect 형식으로 나옴.
    (x, y), (w, h), angle = rect
    # 긴 변을 가로 기준으로 맞추는 보정. 계산을 일괄적으로 긴 변이 밑변이 되도록 설정하였음.
    if w < h:
        angle = angle - 90
    final_angle = round(angle, 1)

    # 도형판별
    # arcLength, 즉, 호의 길이(둘레의길이). 윤곽선좌표를 받아서, 그 도형이 이어져있으면 True임. 다른 외부 도형이 끊어져있다면,
    # False로 인식. 결국 perimeter에 True인것만 남기라는코드
    perimeter = cv2.arcLength(cnt, True)
    #cnt를 재처리. 윤곽선들을 이어가는데, 2%보다 많이 꺽이면 꼭짓점으로 인식하게함. 그 꼭짓점의 좌표들을 approx에 담아줌.
    #전체둘레길이를 줘야, 비율로 인식하게할 수 있어서 perimeter계산한걸 갖다줌. True는 arcLength에서와 동일한 이유.
    approx = cv2.approxPolyDP(cnt, 0.02 * perimeter, True)

    # 이제 Shape는 그냥 도형이름을 말함.
    # approx에서 구한 꼭짓점의 갯수에 따라서 도형을 판별.
    shape = "Etc"
    if len(approx) == 3:
        shape = "Triangle"
    elif len(approx) == 4:
        shape = "Rectangle"
    elif len(approx) > 5:
        # 원 판별 (꼭짓점의 갯수가 5개 이상일때) 샘플이미지1번에서는 8각형까지 원으로 인식해줌.
        # circle_area = cv2.minEnclosingCircle 은 단어 그대로, 가장 작은 외접원을 의미함.
        # 해당 도형을 감싸는 가장 작은 외접원과 면적이 비슷한지를 판별함. 아래에서 오차범위가 20%아래일시:
        # 찌그러져서 원의 성질을 잃지 않았다로 판별 = 원으로 인정해줌.
        (x_c, y_c), radius = cv2.minEnclosingCircle(cnt)
        circle_area = np.pi * radius * radius
        if circle_area > 0 and abs(area - circle_area) / circle_area < 0.2:
            shape = "Circle"

    # 결과 출력 및 인식한 도형을 그려서 보기쉽게 해주기
    if shape != "Etc":
        # 터미널에 순서와 각도 출력
        print(f"{count:<5} | {shape:<10} | ({cx:>4}, {cy:>4}) | {final_angle:>8}°")

        # 윤곽선과 중심점 그리기
        cv2.drawContours(output, [cnt], -1, (0, 255, 0), 2)
        cv2.circle(output, (cx, cy), 5, (0, 0, 255), -1)

        # 도형 이름 앞에 번호(#) 추가
        label = f"#{count} {shape}"
        cv2.putText(output, label, (cx - 40, cy - 10),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 2)

        # 원이 아닐 경우 각도 표시
        if shape != "Circle":
            cv2.putText(output, f"{final_angle}deg", (cx - 40, cy + 15),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 255, 0), 1)

        count += 1

# 5. 결과 출력
cv2.imshow("Edges", edges)
cv2.imshow("Final Result", output)
cv2.waitKey(0)
cv2.destroyAllWindows()
