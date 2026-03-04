#include <iostream>
#include <string>
#include <array>
#include <vector>
#include <regex>

// 입력된 문자열이 정수(숫자)인지 확인하는 함수
bool is_numeric(const std::string& input);


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
	std::array<int, 11> dish_price = { 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11 };
};
struct Drink {
	std::array<std::string, 7> drink_name = { "술1", "술2", "술3", "술4", "술5", "술6", "술7" };
	std::array<int, 7> drink_price = { 1, 2, 3, 4, 5, 6, 7 };
};
int Calculating_Total_Price(std::vector<std::string>& Menu, std::vector<int>& Price);

int main()
{
	Meal meal; Soup soup; Dish dish; Drink drink;
	std::string User_name = "None";
	std::string User_ID;
	std::string Input_Password;
	std::string User_Password = "1234"; // 초기비번. 입력하고나서 바꾸게할 예정.
	int Major_Menu_Picked_By_User = 0; // 대 메뉴 임시
	int Specific_Menu_Picked_By_User = 0; // 소 메뉴 임시
	std::array<int, 4> Menu_major = { 0, 0, 0, 0 }; // 밥 국 반찬 주류 순서
	std::vector<std::string> Bag_Menu;
	std::vector<int> Bag_Price;
	int Born;
	int Total_Price = 0;
	int Order_Done = 0;
	int Payment = 0;
	double Bill_Price = 0;
	int Payed_Bill = 0;
	int Change_Bill = 0;
	std::array <int, 4> Card_Number = { 10, 20, 30, 40 };
	std::array <std::string, 4> Card_Name = { "현대", "삼성", "신한", "국민" };
	int Sales = 0;


	std::cout << "사용자 이름을 입력해주세요. ";
	std::getline(std::cin, User_name);
	std::cout << "\r\n" << "ID를 입력해주세요.  ";
	std::getline(std::cin, User_ID);
	std::cout << "\r\n" << "비밀번호를 입력해주세요. (초기 비밀번호 : 1234)  ";
	std::getline(std::cin, Input_Password);
	if (Input_Password != User_Password)
	{
		for (int i = 0; i < 3; i++)
		{
			if (i==2)
			{
				std::cout << "\r\n" << "비밀번호 3회 불일치로 프로그램을 종료합니다.";
				return 1;
			}
			std::cout << "\r\n" << "다시 입력해주세요. (" << 2-i << "회남음)  ";
			std::getline(std::cin, Input_Password);
			Input_Password.erase(0, Input_Password.find_first_not_of(" \t\n\r\f\v")); // 제미니가 알려줌
			Input_Password.erase(Input_Password.find_last_not_of(" \t\n\r\f\v") + 1); // 첫, 마지막 확인하고 문자가아닌 엔터 등 다 지우기
			// std::cout << "\r\n"<<Input_Password; // 확인용 구문
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


	while (Major_Menu_Picked_By_User != 6)
	{
		std::cout << "\r\n" << "메뉴입니다." << "\r\n" << "1. 밥	2. 국	3. 반찬	4. 주류	5. 주문종료";
		std::cin >> Major_Menu_Picked_By_User;
		switch (Major_Menu_Picked_By_User) {
		case 1:
			if (Menu_major[0] < 2)
			{
				Menu_major[0] += 1;
				for (int i = 0; i < meal.meal_name.size(); i++)
				{
					std::cout << meal.meal_name[i] << "  " << "(" << meal.meal_price[i] <<"원)";
				}
				std::cout << "\r\n" << "밥을 선택하십시오  " << "\r\n";
				std::cin >> Specific_Menu_Picked_By_User;
				Specific_Menu_Picked_By_User = Specific_Menu_Picked_By_User - 1; // 사용자에게 입력은 1 2 3 4 ... 지만, 배열은 0부터 시작하기에 1 빼줌.
				Bag_Menu.push_back(meal.meal_name[Specific_Menu_Picked_By_User]);
				Bag_Price.push_back(meal.meal_price[Specific_Menu_Picked_By_User]);
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
				std::cout << "\r\n" << "국을 선택하십시오  ";
				std::cin >> Specific_Menu_Picked_By_User;
				Specific_Menu_Picked_By_User = Specific_Menu_Picked_By_User - 1; // 사용자에게 입력은 1 2 3 4 ... 지만, 배열은 0부터 시작하기에 1 빼줌.
				Bag_Menu.push_back(soup.soup_name[Specific_Menu_Picked_By_User]);
				Bag_Price.push_back(soup.soup_price[Specific_Menu_Picked_By_User]);
				break;
			}
			std::cout << "\r\n" << "국은 그만!";
			break;
		case 3:
			for (int i = 0; i < dish.dish_name.size(); i++)
			{
				std::cout << dish.dish_name[i] << "  " << dish.dish_price[i];
			}
			std::cout << "\r\n" << "반찬을 선택하십시오  ";
			std::cin >> Specific_Menu_Picked_By_User;
			Specific_Menu_Picked_By_User = Specific_Menu_Picked_By_User - 1; // 사용자에게 입력은 1 2 3 4 ... 지만, 배열은 0부터 시작하기에 1 빼줌.
			Bag_Menu.push_back(dish.dish_name[Specific_Menu_Picked_By_User]);
			Bag_Price.push_back(dish.dish_price[Specific_Menu_Picked_By_User]);
			break;
		case 4:
			while (true)
			{
				std::cout << "생년월일(8자리)를 입력하세요 : ";
				std::cin >> Born;
				if (std::cin.fail())
				{
					std::cin.clear();            // 에러 상태 초기화 // 제미나이가 해줌
					std::cin.ignore(256, '\n'); // 입력 버퍼에 남아있는 더미 값 제거 // 제미나이가해줌
					std::cout << "숫자로만 8자리를 입력해주세요.\n";
					continue;                   // 와일문의 처음(생년월일 입력)으로 돌아감
				}
				if (Born < 19000101)
				{
					std::cout << "올바른 생년월일 형식이 아닙니다.\n";
					continue;                   // 다시 입력받으러 올라감
				}
				// 미성년자 경고멘트 출력만들기
				break;
			}

			for (int i = 0; i < drink.drink_name.size(); i++)
			{
				std::cout <<  drink.drink_name[i] << "  " << drink.drink_price[i];
			}
			std::cout << "\r\n" << "주류를 선택하십시오.  ";
			std::cin >> Specific_Menu_Picked_By_User;
			Specific_Menu_Picked_By_User = Specific_Menu_Picked_By_User - 1; // 사용자에게 입력은 1 2 3 4 ... 지만, 배열은 0부터 시작하기에 1 빼줌.
			Bag_Menu.push_back(drink.drink_name[Specific_Menu_Picked_By_User]);
			Bag_Price.push_back(drink.drink_price[Specific_Menu_Picked_By_User]);
			break;
		case 5:
			std::cout << "주문을 종료하시겠습니까?" << "\r\n" << "현재 장바구니 담아두신 품목은" << "\r\n";
			Total_Price = Calculating_Total_Price(Bag_Menu, Bag_Price);
			std::cout << "총 금액은 : " << Total_Price << "원 입니다.";
			std::cout << "주문을 종료하시려면 1, 다시 처음부터 주문하시려면 2를 입력해주세요.(단, 2를 입력하시면 장바구니가 초기화됩니다.) : ";
			std::cin >> Order_Done;
			while (true)
			{
				if (std::cin.fail())
				{
					std::cin.clear();            // 에러 상태 초기화 // 제미나이가 해줌
					std::cin.ignore(256, '\n'); // 입력 버퍼에 남아있는 더미 값 제거 // 제미나이가해줌
					std::cout << "1 혹은 2로 입력해주세요.\n";
					continue;                   // 와일문의 처음(생년월일 입력)으로 돌아감
				}
				if ((Order_Done < 1) || (Order_Done>2))
				{
					std::cout << "1 혹은 2로 입력해주세요.\n";
					continue;                   // 다시 입력받으러 올라감
				}
				break;
			}
			if (Order_Done == 1)
			{
				Major_Menu_Picked_By_User = 6;
				break;
			}
			//장바구니(메뉴, 가격) 전부 초기화.
			break;
		}
	}
	//현금 카드 결제수단 선택
	std::cout << "결제수단이 현금이시면 1, 카드시면 2 를 눌러주세요.";
	std::cin >> Payment;
	if (Payment == 1)
	{
		Bill_Price = Total_Price * 0.9;
		std::cout << "결제 금액은 " << Bill_Price << "입니다." << "\r\n" << "투입하실 금액을 입력해주세요 : \r\n";
		std::cin >> Payed_Bill;
		Change_Bill = Payed_Bill - Bill_Price;
		if (Change_Bill < 0)
		{

		}
	}

	//현금은 10% 할인
	//카드는 카드번호 4자리 입력받기, 앞 2자리에 따라 카드종류 구분


	
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

bool is_numeric(const std::string& input) {
	// ^: 시작, \d+: 숫자 1개 이상, $: 끝
	// 만약 음수까지 허용하려면 R"(^[+-]?\d+$)" 로 수정하세요.
	static const std::regex int_pattern(R"(^\d+$)");
	return std::regex_match(input, int_pattern);
}
