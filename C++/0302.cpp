#include <iostream>
#include <string>
#include <array>
#include <vector>
#include <regex> 

bool is_numeric(const std::string& input)
{
    static const std::regex int_pattern(R"(^\d+$)");
    return std::regex_match(input, int_pattern);
}

// 전체 검사 함수
bool IsNumericOverall(const std::string& input)
{
    // 1️⃣ 숫자 형식 검사 (기존 함수 호출)
    if (!is_numeric(input))
        return false;

    // 2️⃣ int 최대값: 2147483647 (10자리)

    // 10자리 초과면 overflow
    if (input.length() > 10)
        return false;

    // 10자리면 최대값 비교
    if (input.length() == 10 && input > "2147483647")
        return false;

    return true;
}
bool isAdult()
{
    std::string Checking;
    int Born_Year;
    while (true)
    {
        std::cout << "생년월일(8자리)를 입력하세요 : ";
        std::cin >> Checking;
        if (!IsNumericOverall(Checking))
        {
            std::cout << "숫자로만 8자리를 입력해주세요.\n";
            continue;
        }
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
        if (!IsNumericOverall(input_string)) {
            std::cout << "1 ~ " << size_of_array << " 사이의 숫자로만 입력해주세요. ";
            continue;
        }
        string_to_int = std::stoi(input_string);
        if ((string_to_int <= 0) || (string_to_int > size_of_array)) {
            std::cout << "1 ~ " << size_of_array << " 사이의 숫자로만 입력해주세요. ";
            continue;
        }
        break;
    }
    return string_to_int - 1;
}

struct Meal
{
    std::array<std::string, 5> meal_name = { "오므라이스", "백반", "볶음밥", "카레라이스", "비빔밥" };
    std::array<int, 5> meal_price = { 1000, 2000, 3000, 4000, 5000 };
};
struct Soup
{
    std::array<std::string, 5> soup_name = { "국1", "국2", "국3", "국4", "국5" };
    std::array<int, 5> soup_price = { 1000, 2000, 3000, 4000, 5000 };
};
struct Dish
{
    std::array<std::string, 11> dish_name = { "반1", "반2", "반3", "반4", "반5", "반6", "반7", "반8", "반9", "반10", "반11" };
    std::array<int, 11> dish_price = { 1000, 1000, 1000, 1000, 1000, 1000, 1000, 1000, 1000, 1000, 1000 };
};
struct Drink
{
    std::array<std::string, 7> drink_name = { "술1", "술2", "술3", "술4", "술5", "술6", "술7" };
    std::array<int, 7> drink_price = { 1000, 2000, 3000, 4000, 5000, 6000, 7000 };
};

struct Card_info
{
    std::string Card_Name;
    int Card_Num;
};

std::vector<Card_info> Card_list = {{"현대", 10}, {"삼성", 20}, {"신한", 30}, {"국민", 40}};

struct user_info
{
    std::string name;
    std::string id;
    std::string pw;
};
std::vector<user_info> g_user_list;

void AddUser(std::string name, std::string id, std::string pw)
{
    user_info new_user = { name, id, pw };
    g_user_list.push_back(new_user);
}

int Calculating_Total_Price(std::vector<std::string>& Menu, std::vector<int>& Price)
{
    int Total = 0;
    for (size_t i = 0; i < Menu.size(); i++)
    {
        std::cout << Menu[i] << " : " << Price[i] << "원\r\n";
        Total += Price[i];
    }
    return Total;
}

int main()
{
    // --- [1. 변수 선언부 (최상단 배치)] ---
    Meal meal; 
    Soup soup; 
    Dish dish; 
    Drink drink;
    
    std::string user_name = "None";
    std::string user_id;
    std::string input_password;
    std::string user_password = "1234";
    std::string input_check_needed;
    std::string card_name = "";
    
    int major_menu_picked_by_user = 0;
    int specific_menu_picked_by_user = 0;
    int total_price = 0;
    int sales = 0;
    int payment = 0;
    int final_price = 0;
    int payed_bill = 0;
    int card_code = 0;
    bool is_payed_succesfully = false;
    double discount_rate = 0.0;
    
    std::array<int, 4> menu_major = { 0, 0, 0, 0 };
    std::vector<std::string> bag_menu;
    std::vector<int> bag_price;

    // --- [2. 프로그램 로직 시작] ---
    while (true)
    {
        std::cout << "사용자 이름을 입력해주세요. ";
        std::getline(std::cin, user_name);
        std::cout << "\r\nID를 입력해주세요. ";
        std::getline(std::cin, user_id);
        std::cout << "\r\n비밀번호를 입력해주세요. (초기: 1234) ";
        user_password = "1234";
        std::getline(std::cin, input_password);

        if (input_password != user_password) {
            for (int i = 0; i < 3; i++) {
                if (i == 2) {
                    std::cout << "\r\n비밀번호 3회 불일치로 프로그램을 종료합니다.";
                    return 1;
                }
                std::cout << "\r\n다시 입력해주세요. (" << 2 - i << "회 남음) ";
                std::getline(std::cin, input_password);
                input_password = trim(input_password); 
                if (input_password == user_password) break;
            }
        }

        std::cout << "\r\n재설정할 비밀번호를 입력하십시오 : ";
        std::getline(std::cin, user_password);
        user_password = trim(user_password);
        std::cout << "\r\n비밀번호가 변경되었습니다.";
        std::cout << "\r\n----------------------------------";
        std::cout << "\r\n환영합니다 000 0000 전문점 입니다.";
        std::cout << "\r\n----------------------------------";

        AddUser(user_name, user_id, user_password);
        major_menu_picked_by_user = 0;

        while (major_menu_picked_by_user != 6)
        {
            std::cout << "\r\n메뉴입니다." << "\r\n" << "1. 밥  2. 국   3. 반찬 4. 주류 5. 주문종료" << "\r\n" << "원하시는 품목을 선택해주세요. ";
            std::cin >> input_check_needed;
            if (!IsNumericOverall(input_check_needed)) {
                std::cout << "숫자만 입력해주세요.\n";
                continue;
            }
            major_menu_picked_by_user = std::stoi(input_check_needed);

            if (major_menu_picked_by_user == 99) {
                std::cout << "\r\n\r\n\r\n\r\n매출은 : " << sales << "원 입니다.";
                return 0;
            }

            switch (major_menu_picked_by_user) {
            case 1:
                if (menu_major[0] < 2)
                {
                    menu_major[0] += 1;
                    for (size_t i = 0; i < meal.meal_name.size(); i++)
                    std::cout << meal.meal_name[i] << "  (" << meal.meal_price[i] << "원) ";
                    specific_menu_picked_by_user = MenuPicker((int)meal.meal_name.size());
                    bag_menu.push_back(meal.meal_name[specific_menu_picked_by_user]);
                    bag_price.push_back(meal.meal_price[specific_menu_picked_by_user]);
                }
                else std::cout << "\r\n밥은 그만!\n";
                break;
            case 2:
                if (menu_major[1] < 3) {
                    menu_major[1] += 1;
                    for (size_t i = 0; i < soup.soup_name.size(); i++) std::cout << soup.soup_name[i] << "  " << soup.soup_price[i] << " ";
                    specific_menu_picked_by_user = MenuPicker((int)soup.soup_name.size());
                    bag_menu.push_back(soup.soup_name[specific_menu_picked_by_user]);
                    bag_price.push_back(soup.soup_price[specific_menu_picked_by_user]);
                }
                else std::cout << "\r\n국은 그만!\n";
                break;
            case 3:
                menu_major[2] += 1;
                for (size_t i = 0; i < dish.dish_name.size(); i++) std::cout << dish.dish_name[i] << "  " << dish.dish_price[i] << " ";
                specific_menu_picked_by_user = MenuPicker((int)dish.dish_name.size());
                bag_menu.push_back(dish.dish_name[specific_menu_picked_by_user]);
                bag_price.push_back(dish.dish_price[specific_menu_picked_by_user]);
                break;
            case 4:
                if (isAdult()) {
                    menu_major[3] += 1;
                    for (size_t i = 0; i < drink.drink_name.size(); i++) std::cout << drink.drink_name[i] << "  " << drink.drink_price[i] << " ";
                    specific_menu_picked_by_user = MenuPicker((int)drink.drink_name.size());
                    bag_menu.push_back(drink.drink_name[specific_menu_picked_by_user]);
                    bag_price.push_back(drink.drink_price[specific_menu_picked_by_user]);
                }
                break;
            case 5:
                std::cout << "주문을 종료하시겠습니까?" << "\r\n" << "현재 장바구니 담아두신 품목은" << "\r\n";
                total_price = Calculating_Total_Price(bag_menu, bag_price);
                std::cout << "\r\n총 금액은 : " << total_price << "원 입니다.";
                std::cout << "\r\n주문을 종료하시려면 1, 다시 처음부터 주문하시려면 2를 입력해주세요 : ";
                while (true)
                {
                    std::cin >> input_check_needed;
                    if (input_check_needed == "1")
                    {
                        major_menu_picked_by_user = 6;
                        break;
                    }
                    if (input_check_needed == "2")
                    {
                        break;
                    }
                    std::cout << "1 혹은 2로 입력해주세요.\n";
                }
                break;
            case 99:
                std::cout << "매출은" << final_price <<"원 입니다.\r\n";
                return 0;
            default:
                std::cout << "1과 5 사이의 숫자로 입력해주세요. \r";
                major_menu_picked_by_user = 0;
                break;
            }
        }

        // --- [합산 할인 및 결제 처리] ---
        discount_rate = 0.0;
        if (menu_major[2] >= 9)
        {
            std::cout << "확인용 반찬개수: " << menu_major[2] << "\r\n";
            std::cout << "반찬을 9가지 이상 주문하셔서 20%를 할인 예약합니다.\r\n";
            discount_rate += 0.2;
        }


        while (!is_payed_succesfully)
        {
            std::cin.ignore(1000, '\n');
            std::cout << "결제수단이 현금이시면 1, 카드시면 2 를 눌러주세요.";
            payment = 0;
            std::cin >> input_check_needed;
            if (!IsNumericOverall(input_check_needed))
            {
                std::cout << "1 혹은 2의 숫자로만 입력해 주세요! \r\n";
                continue;
            }
            payment = std::stoi(input_check_needed);
            if (payment == 1)
                {
                    std::cout << "현금 결제 10% 추가 할인이 예약되었습니다.\n";
                    discount_rate += 0.1;
                }

            final_price = (int)(total_price * (1.0 - discount_rate));
            std::cout << "최종 결제 금액은 " << final_price << "원 입니다. (총 할인: " << (int)(discount_rate * 100) << "%)\n";
                if (payment == 1) // 현금
                {
                    std::cin.ignore(1000, '\n');
                    payed_bill = 0;
                    while (true)
                    {
                        std::cout << "투입하실 금액을 입력해주세요\r\n(결제수단을 다시 선택하시려면 0을 입력해주세요.)\r\n투입 금액 : ";
                        std::cin >> input_check_needed;
                        if (!IsNumericOverall(input_check_needed))
                        {
                            std::cout << "잘못된 입력입니다.\n";
                            continue;
                        }
                        else if (IsNumericOverall(input_check_needed))
                        {
                            payed_bill = std::stoi(input_check_needed);
                            if (payed_bill == 0)
                            {
                                discount_rate -=0.1;
                                break;
                            }
                            
                            if (payed_bill < final_price)
                            {
                                std::cout << "금액이 부족합니다! 투입하실 금액을 다시 입력해주세요.\r\n";
                                continue;
                            }
                            if (payed_bill >= final_price) 
                            {
                                std::cout << "결제되었습니다.\n";
                                std::cout << "거스름돈은 " << payed_bill - final_price << "원 입니다.\r\n";
                                is_payed_succesfully = true;
                                break;
                            }
                        }
                    }

                    
                }
                else if (payment == 2)// 카드
                { 
                    while (true)
                    {
                        std::cout << "카드 번호 4자리를 입력해주세요 (취소: 0000): ";
                        std::cin >> input_check_needed;
                        if (IsNumericOverall(input_check_needed) && input_check_needed.length() == 4)
                        {
                            if (input_check_needed == "0000")
                            {
                                std::cout << "결제 취소.\n";
                                break;
                            }
                            card_code = std::stoi(input_check_needed) / 100;
                            card_name = "";
                            for (const auto& code_saved : Card_list)
                            {
                                if (code_saved.Card_Num == card_code)
                                {
                                    card_name = code_saved.Card_Name;
                                }
                            }
                            if (!card_name.empty())
                            {
                                std::cout << ">> [" << card_name << "카드] 인증 성공! 결제 완료.\n";
                                is_payed_succesfully = true;
                                break;
                            }
                            else std::cout << ">> 지원하지 않는 카드사 코드입니다.\n";
                        }
                        else std::cout << ">> 숫자로 된 4자리 번호만 입력 가능합니다.\n";
                        continue;
                    }
                }
        }
        
        


        is_payed_succesfully = false; // 결제상태 다시 초기화
        sales += final_price; // 매출기록
        

        // 리셋 세트
        menu_major.fill(0);
        bag_menu.clear();
        bag_price.clear();
        total_price = 0;
        std::cin.ignore(1000, '\n');
    }
    return 0;
}
