#include <iostream>
#include <string>
#include <array>
#include <vector>
#include <regex> // 정규표현식 사용을 위해 추가

// --- [추가] 정수 여부 확인 함수 ---
bool is_numeric(const std::string& input) {
	static const std::regex int_pattern(R"(^\d+$)");
	return std::regex_match(input, int_pattern);
}
bool isAdult() {
	std::string Checking;
	int Born_Year;
	while (true)
	{
		std::cout << "생년월일(8자리)를 입력하세요 : ";
		// [수정] 정규표현식 적용
		std::cin >> Checking;
		if (!is_numeric(Checking))
		{
			std::cout << "숫자로만 8자리를 입력해주세요.\n";
			continue;
		}
		if (is_numeric(Checking))
		{
			if (Checking.size() != 8)
			{
				std::cout << "올바른 생년월일 형식이 아닙니다.\n";
				continue;
			}
			Born_Year = std::stoi(Checking);
			if (Born_Year < 19000101)
			{
				std::cout << "올바른 생년월일 형식이 아닙니다.\n";
				continue;
			}
			if ((2026 - (Born_Year / 10000)) < 19)
			{
				std::cout << "미성년자에게는 주류를 판매하지 않습니다! \r\n";
				return false;
			}
			break;
		}
	}
	return true;
}

int MenuPicker(int size_of_array)
{
	std::string input_string;
	int string_to_int = 0;
	while (true)
	{
		std::cout << "원하시는 품목을 선택해 주십시오. \r\n";
		std::cin >> input_string;
		if (!is_numeric(input_string))
		{
			std::cout << "1 ~ " << size_of_array << " 사이의 숫자로만 입력해주세요. ";
			continue;
		}

		if (is_numeric(input_string))
		{
			string_to_int = 1;
			string_to_int = std::stoi(input_string);
			if ((string_to_int < 0) || (string_to_int > size_of_array))
			{
				std::cout << "1 ~ " << size_of_array << " 사이의 숫자로만 입력해주세요. ";
				continue;
			}
		}
		break;
	}
	string_to_int = string_to_int - 1;
	return string_to_int;
}


struct Meal {
	std::array<std::string, 5> meal_name = { "오므라이스", "백반", "볶음밥", "카레라이스", "비빔밥" };
	std::array<int, 5> meal_price = { 1, 2, 3, 4, 5 };
};
struct Soup {
	std::array<std::string, 5> soup_name = { "국1", "국2", "국3", "국4", "국5" };
	std::array<int, 5> soup_price = { 1, 2, 3, 4, 5 };
};
struct Dish {
	std::array<std::string, 11> dish_name = { "반1", "반2", "반3", "반4", "반5", "반6", "반7", "반8", "반9", "반10", "반11" };
	std::array<int, 11> dish_price = { 10, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11 };
};
struct Drink {
	std::array<std::string, 7> drink_name = { "술1", "술2", "술3", "술4", "술5", "술6", "술7" };
	std::array<int, 7> drink_price = { 1, 2, 3, 4, 5, 6, 7 };
};
struct Card_info {
	std::string Card_Name;
	int Card_Num;
};

// 2. 설계도를 바탕으로 실제 데이터 목록 만들기
// 이제 Card_list[0]은 현대/10, [1]은 삼성/20... 이렇게 저장됩니다.
std::vector<Card_info> Card_list = {
	{"현대", 10},
	{"삼성", 20},
	{"신한", 30},
	{"국민", 40}
};

struct user_info
{
	std::string name;
	std::string id;
	std::string pw;
};
std::vector<user_info> g_user_list;

void AddUser(std::string name, std::string id, std::string pw);

int Calculating_Total_Price(std::vector<std::string>& Menu, std::vector<int>& Price);

int main()
{
	int g_user_count = 0;
	Meal meal; Soup soup; Dish dish; Drink drink;
	std::string User_name = "None";
	std::string User_ID;
	std::string Input_Password;
	std::string User_Password = "1234";
	int Major_Menu_Picked_By_User = 0;
	int Specific_Menu_Picked_By_User = 0;
	std::array<int, 4> Menu_major = { 0, 0, 0, 0 }; // 주문량 확인을 위한 배열
	std::vector<std::string> Bag_Menu;
	std::vector<int> Bag_Price;
	bool is_adult;
	int price_for_show = 0;
	int Total_Price = 0;
	int Order_Done = 0;
	int Payment = 0;
	int Discount_Number = 0; // 현금결제시 10%, 반찬 9개이상 선택시 20%. 할인율 끼리는 합연산으로 더해서 최종 결제금액에 적용.
	double Bill_Price; //현금가 10% 디씨
	int Payed_Bill = 0;
	int Change_Bill = 0;
	int Card_Number = 0;
	int Input_Card_Number;
	int Card_Company_Code;
	
	int Sales = 0;

	std::string Input_Check_Needed; // [추가] 정규식 검사를 위한 임시 문자열 변수
	while (true)
	{
		std::cout << "사용자 이름을 입력해주세요. ";
		std::getline(std::cin, User_name);
		std::cout << "\r\n" << "ID를 입력해주세요.  ";
		std::getline(std::cin, User_ID);
		std::cout << "\r\n" << "비밀번호를 입력해주세요. (초기 비밀번호 : 1234)  ";
		User_Password = "1234";
		std::getline(std::cin, Input_Password);
		if (Input_Password != User_Password)
		{
			for (int i = 0; i < 3; i++)
			{
				if (i == 2)
				{
					std::cout << "\r\n" << "비밀번호 3회 불일치로 프로그램을 종료합니다.";
					return 1;
				}
				std::cout << "\r\n" << "다시 입력해주세요. (" << 2 - i << "회남음)  ";
				std::getline(std::cin, Input_Password);
				Input_Password.erase(0, Input_Password.find_first_not_of(" \t\n\r\f\v"));
				Input_Password.erase(Input_Password.find_last_not_of(" \t\n\r\f\v") + 1);
				if (Input_Password == User_Password)
				{
					break;
				}
			}

		}

		std::cout << "\r\n" << "재설정할 비밀번호를 입력하십시오 : ";
		std::getline(std::cin, User_Password);
		User_Password.erase(0, User_Password.find_first_not_of(" \t\n\r\f\v"));
		User_Password.erase(User_Password.find_last_not_of(" \t\n\r\f\v") + 1);
		std::cout << "\r\n" << "비밀번호가 변경되었습니다.";
		std::cout << "\r\n" << "----------------------------------";
		std::cout << "\r\n" << "환영합니다 000 0000 전문점 입니다.";
		std::cout << "\r\n" << "----------------------------------";


		AddUser(User_name, User_ID, User_Password);

		while (Major_Menu_Picked_By_User != 6)
		{
			std::cout << "\r\n" << "메뉴입니다." << "\r\n" << "1. 밥	2. 국	3. 반찬	4. 주류	5. 주문종료" << "\r\n" << "원하시는 품목을 선택해주세요. ";

			// [수정] 정규표현식 적용
			std::cin >> Input_Check_Needed;
			if (!is_numeric(Input_Check_Needed)) {
				std::cout << "숫자만 입력해주세요.\n";
				continue;
			}
			Major_Menu_Picked_By_User = std::stoi(Input_Check_Needed);

			switch (Major_Menu_Picked_By_User) {
			case 1:
				if (Menu_major[0] < 2)
				{
					Menu_major[0] += 1; // 식사류 카운트 +1
					for (int i = 0; i < meal.meal_name.size(); i++)
					{
						std::cout << meal.meal_name[i] << "  " << "(" << meal.meal_price[i] << "원)";
					}
					//std::cout << "\r\n" << "밥을 선택하십시오  " << "\r\n";
					Input_Check_Needed = MenuPicker(static_cast<int>(meal.meal_name.size()));
					// [수정] 정규표현식 적용
					//std::cin >> Input_Check_Needed;
					if (is_numeric(Input_Check_Needed)) {
						Specific_Menu_Picked_By_User = std::stoi(Input_Check_Needed) - 1;
						Bag_Menu.push_back(meal.meal_name[Specific_Menu_Picked_By_User]);
						Bag_Price.push_back(meal.meal_price[Specific_Menu_Picked_By_User]);
					}
					break;
				}
				std::cout << "\r\n" << "밥은 그만!";
				break;
			case 2:
				if (Menu_major[1] < 3)
				{
					Menu_major[1] += 1;
					for (int i = 0; i < soup.soup_name.size(); i++)
					{
						std::cout << soup.soup_name[i] << "  " << soup.soup_price[i];
					}
					Input_Check_Needed = MenuPicker(static_cast<int>(soup.soup_name.size()));
					if (is_numeric(Input_Check_Needed)) {
						Specific_Menu_Picked_By_User = std::stoi(Input_Check_Needed) - 1;
						Bag_Menu.push_back(soup.soup_name[Specific_Menu_Picked_By_User]);
						Bag_Price.push_back(soup.soup_price[Specific_Menu_Picked_By_User]);
					}
					break;
				}
				std::cout << "\r\n" << "국은 그만!";
				break;
			case 3:
				Menu_major[2] += 1;
				for (int i = 0; i < dish.dish_name.size(); i++)
				{
					std::cout << dish.dish_name[i] << "  " << dish.dish_price[i];
				}
				Input_Check_Needed = MenuPicker(static_cast<int>(dish.dish_name.size()));
				if (is_numeric(Input_Check_Needed)) {
					Specific_Menu_Picked_By_User = std::stoi(Input_Check_Needed) - 1;
					Bag_Menu.push_back(dish.dish_name[Specific_Menu_Picked_By_User]);
					Bag_Price.push_back(dish.dish_price[Specific_Menu_Picked_By_User]);
				}
				break;
			case 4:
				is_adult = isAdult();

				if (!is_adult)
				{
					break;
				}
				Menu_major[3] += 1;
				for (int i = 0; i < drink.drink_name.size(); i++)
				{
					std::cout << drink.drink_name[i] << "  " << drink.drink_price[i];
				}
				Input_Check_Needed = MenuPicker(static_cast<int>(drink.drink_name.size()));
				if (is_numeric(Input_Check_Needed)) {
					Specific_Menu_Picked_By_User = std::stoi(Input_Check_Needed) - 1;
					Bag_Menu.push_back(drink.drink_name[Specific_Menu_Picked_By_User]);
					Bag_Price.push_back(drink.drink_price[Specific_Menu_Picked_By_User]);
				}
				break;
			case 5:
				std::cout << "주문을 종료하시겠습니까?" << "\r\n" << "현재 장바구니 담아두신 품목은" << "\r\n";
				Total_Price = Calculating_Total_Price(Bag_Menu, Bag_Price);
				std::cout << Menu_major[2] << "\r\n";
				std::cout << "총 금액은 : " << Total_Price << "원 입니다.";
				std::cout << "\r\n주문을 종료하시려면 1, 다시 처음부터 주문하시려면 2를 입력해주세요 : ";

				// [수정] 정규표현식 적용
				while (true)
				{
					std::cin >> Input_Check_Needed;
					if (!is_numeric(Input_Check_Needed))
					{
						std::cout << "1 혹은 2로 입력해주세요.\n";
						continue;
					}
					Order_Done = std::stoi(Input_Check_Needed);
					if ((Order_Done < 1) || (Order_Done > 2))
					{
						std::cout << "1 혹은 2로 입력해주세요.\n";
						continue;
					}
					break;
				}
				if (Order_Done == 1)
				{
					Major_Menu_Picked_By_User = 6;
					break;
				}
				break;
			case 99:
				std::cout << "\r\n\r\n\r\n\r\n 매출은 : " << Sales << "원 입니다.";
				return 0;
			default:
				std::cout << "1과 5 사이의 숫자로 입력해주세요. \r";
				Major_Menu_Picked_By_User = 7;
				Bag_Menu.clear();
				Bag_Price.clear();
				Menu_major.fill(0);
				break;
			}
		}


		std::cout << "확인용" << Menu_major[2] << "\r\n";
		if (Menu_major[2] >= 9)
		{
			Discount_Number += 2;//반찬 9가지이상 주문 시 20%할인
			price_for_show = Total_Price * (1 - (Discount_Number * 0.1));
			std::cout << "반찬을 9가지 이상 주문하셔서 총 금액에서 20%를 할인해 드립니다. \r\n최종금액은 : " << Total_Price << "원 입니다.";
		}
		std::cout << "결제수단이 현금이시면 1, 카드시면 2 를 눌러주세요.";

		// [수정] 정규표현식 적용
		std::cin >> Input_Check_Needed;
		if (is_numeric(Input_Check_Needed)) Payment = std::stoi(Input_Check_Needed);

		if (Payment == 1)
		{
			Discount_Number += 1;
			Bill_Price = Total_Price * (1 - (Discount_Number * 0.1));
			std::cout << "현금으로 결제를 선택하셨습니다 \r\n 결제 금액은 " << Bill_Price << "입니다." << "\r\n";
			// [수정] 정규표현식 적용

			while (true)
			{
				std::cout << "투입하실 금액을 입력해주세요 : \r\n";
				std::cin >> Input_Check_Needed;
				if (is_numeric(Input_Check_Needed))
				{
					Payed_Bill = std::stoi(Input_Check_Needed);
					break;
				}

				else
				{
					std::cout << "잘못된 입력입니다. \r\n";
					std::cin.clear(); // 입력 버퍼 청소 (문자 입력 대비)
					std::cin.ignore(1000, '\n');
					continue;
				}
			}


			Change_Bill = Payed_Bill - (int)Bill_Price;
			if (Change_Bill < 0)
			{
				std::cout << "금액이 부족합니다.";
			}
			else {
				std::cout << "거스름돈은 " << Change_Bill << "원 입니다.";
			}
		}
		else if (Payment == 2) //제미나이 도움 받음
		{
			std::cout << "카드로 결제를 선택하셨습니다.\n";

			while (true)
			{
				std::cout << "카드 번호 4자리를 입력해주세요 (취소: 0000): ";
				std::cin >> Input_Check_Needed;

				// 1. 형식 검사 (숫자 4자리)
				if (is_numeric(Input_Check_Needed) && Input_Check_Needed.length() == 4)
				{
					if (Input_Check_Needed == "0000") {
						std::cout << "결제가 취소되었습니다.\n";
						break;
					}

					Input_Card_Number = std::stoi(Input_Check_Needed);
					Card_Company_Code = Input_Card_Number / 100; // 앞 2자리 정수 추출

					std::string Validated_Card_Name = "";
					// 2. 카드 목록(구조체 리스트)에서 일치하는 코드 찾기
					for (const auto& card : Card_list) {
						if (card.Card_Num == Card_Company_Code)
						{
							Validated_Card_Name = card.Card_Name;
							break;
						}
					}

					// 3. 검증 결과 확인
					if (!Validated_Card_Name.empty()) { // 이름이 비어있지 않다면(찾았다면)
						Card_Number = Input_Card_Number;
						std::cout << ">> [" << Validated_Card_Name << "카드] 인증 성공!\n";
						Total_Price = price_for_show;
						std::cout << ">> "<< Total_Price << "원 결제가 완료되었습니다. 감사합니다!\n";

						// 여기서 장바구니 초기화 등의 로직으로 넘어감
						break;
					}
					else {
						std::cout << ">> 오류: 지원하지 않는 카드사 코드(" << Card_Company_Code << ")입니다.\n";
					}
				}
				else {
					std::cout << ">> 오류: 숫자로 된 '4자리' 번호만 입력 가능합니다!\n";
				}
			}
		}

		g_user_count++;
		Sales += Total_Price;
		Specific_Menu_Picked_By_User = 0;
		Menu_major.fill(0);
		Bag_Menu.clear();
		Bag_Price.clear();
		price_for_show = 0;
		Total_Price = 0;
		Discount_Number = 0;
		Order_Done = 0;
		Payment = 0;
		std::cin.ignore(1000, '\n');

	}
	std::cout << "\r\n\r\n\r\n\r\n 매출은 : " << Sales << "원 입니다.";
	return 0;
}

int Calculating_Total_Price(std::vector<std::string>& Menu, std::vector<int>& Price)
{
	int Total_Price = 0;
	for (int i = 0; i < Menu.size(); i++)
	{
		std::cout << Menu[i] << " : " << Price[i] << "\r\n";	
		Total_Price += Price[i];
	}
	return Total_Price;
}

void AddUser(std::string name, std::string id, std::string pw) {
	user_info new_user;
	new_user.name = name;
	new_user.id = id;
	new_user.pw = pw;

	g_user_list.push_back(new_user); // 세트로 벡터에 추가
}
