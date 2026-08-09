#include <iostream>
#include <string> //문자열사용을 위한 헤더파일
#include <array> //배열사용을 위한 헤더파일
#include <vector> //벡터사용을 위한 헤더파일
#include <regex> //정규표현식을 위한 헤더파일
#include <fstream> //파일에 입출력을 위한 헤더파일
#include <ctime> //시간
#include <iomanip> //시간을 이쁘게쓰기

#include <termios.h> // 리눅스 터미널 설정용
#include <unistd.h>  // STDIN_FILENO용

#include <iostream>
#include <termios.h>
#include <unistd.h>
#include <string>

struct TerminalRestorer {
    struct termios oldt;
    bool success; // 설정 성공 여부 기록

    TerminalRestorer() : success(false) {
        if (tcgetattr(STDIN_FILENO, &oldt) == -1) {
            perror("tcgetattr 실패");
            return;
        }
        // 진입 시 이전 입력 찌꺼기 완벽 제거
        tcflush(STDIN_FILENO, TCIFLUSH);
        
        struct termios newt = oldt;
        // ECHO와 ICANON 끄기
        newt.c_lflag &= ~(ECHO | ICANON);
        
        // VMIN: 최소 1글자 읽을 때까지 기다림
        // VTIME: 0 대기시간 없음
        newt.c_cc[VMIN] = 1; 
        newt.c_cc[VTIME] = 0;

        if (tcsetattr(STDIN_FILENO, TCSANOW, &newt) == -1) {
            perror("tcsetattr 실패");
            return;
        }
        success = true;
    }

    ~TerminalRestorer() {
        if (success) {
            tcsetattr(STDIN_FILENO, TCSANOW, &oldt);
        }
    }
};

std::string get_masked_input() {
    // 출력 버퍼를 즉시 비워서 문구가 화면에 바로 나타나게 함
    std::cout << std::flush;

    TerminalRestorer restorer; 
    
    // 만약 터미널 설정에 실패했다면 함수 조기 종료
    if (!restorer.success) {
        std::cerr << "터미널 설정을 변경할 수 없습니다." << std::endl;
        std::string temp;
        std::getline(std::cin, temp); 
        return temp;
    }

    std::string password;
    char ch;

    while (read(STDIN_FILENO, &ch, 1) == 1) {
        if (ch == '\n' || ch == '\r') break;
        
        if (ch == 127) { // 백스페이스 처리
            if (!password.empty()) {
                std::cout << "\b \b" << std::flush;
                password.pop_back();
            }
        } else {
            password += ch;
            std::cout << '*' << std::flush;
        }
    }
    std::cout << std::endl;
    
    // 함수 종료 시에도 버퍼를 깨끗하게 비워 다음 작업 보호
    tcflush(STDIN_FILENO, TCIFLUSH);
    return password;
}

// 제미니도움받음
bool is_numeric(const std::string& input)
{
    static const std::regex int_pattern(R"(^\d+$)");
    return std::regex_match(input, int_pattern);
}

// 전체 검사 함수 -- 제미니도움받음
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


//미성년자 구별함수
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
//메뉴선택 함수
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
// 메뉴구조체
struct Meal
{
    std::array<std::string, 5> meal_name = { "밥1", "밥2", "밥3", "밥4", "밥5" };
    std::array<int, 5> meal_price = { 3000, 2000, 3000, 4000, 5000 };
};
struct Soup
{
    std::array<std::string, 5> soup_name = { "국1", "국2", "국3", "국4", "국5" };
    std::array<int, 5> soup_price = { 1500, 2000, 3500, 4000, 5000 };
};
struct Dish
{
    std::array<std::string, 11> dish_name = { "반1", "반2", "반3", "반4", "반5", "반6", "반7", "반8", "반9", "반10", "반11" };
    std::array<int, 11> dish_price = { 1500, 2000, 2500, 1000, 1000, 4000, 1000, 1000, 1000, 5000, 1000 };
};
struct Drink
{
    std::array<std::string, 7> drink_name = { "술1", "술2", "술3", "술4", "술5", "술6", "술7" };
    std::array<int, 7> drink_price = { 1000, 2000, 3000, 4000, 5000, 6000, 7000 };
};
//카드정보
struct Card_info
{
    std::string Card_Name;
    int Card_Num;
};

std::vector<Card_info> Card_list = { {"현대", 10}, {"삼성", 20}, {"신한", 30}, {"국민", 40} };
//회원가입 정보 분류
struct user_info
{
    std::string name;
    std::string id;
    std::string pw;
};
std::vector<user_info> g_user_list;
//회원가입정보입력함수
void AddUser(std::string name, std::string id, std::string pw)
{
    user_info new_user = { name, id, pw };
    g_user_list.push_back(new_user);
}
//가격계산함수
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
// --- 문자열 공백 제거 함수 --- gemini
std::string trim(const std::string& s) {
    size_t first = s.find_first_not_of(" \t\n\r\f\v");
    if (first == std::string::npos) return ""; // 공백만 있는 경우
    size_t last = s.find_last_not_of(" \t\n\r\f\v");
    return s.substr(first, (last - first + 1));
}

//로그인 함수
int Login(const std::string& idcheck, const std::string& pwcheck, const std::vector<user_info>& user_list)
{

    for (int i = 0; i < user_list.size(); i++)
    {
        if (idcheck == user_list[i].id)
            if (pwcheck == user_list[i].pw)
            {
                return i;
            }
    }
    return -1;
}
void Update_Total_Sales(int new_sales)
{
    std::ofstream outFile("Sales.csv", std::ios::app);
    std::time_t now = std::time(nullptr);
    outFile << std::put_time(std::localtime(&now), "%Y-%m-%d %H:%M:%S")<< "," << new_sales << "\n";
    outFile.close();

}

int main()
{
    const int BUFFER_SIZE = 1000;
    const int NOT_USER = -1;

    //변수선언부
    Meal meal;
    Soup soup;
    Dish dish;
    Drink drink;
    bool is_running = true;
    std::string user_name = "None";
    std::string user_id;
    std::string input_password;
    std::string user_password = "1234";
    std::string input_check_needed;
    std::string card_name = "";
    int user_checking = 0;
    std::string id_for_checking;
    std::string pw_for_checking;
    int user_number = NOT_USER;
    bool correct_password = false;

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
    
    //전체로직시작
    while (true)
    {
        while (is_running)
        {
            std::cout << "-----------------안녕하세요-----------------\r\n기존 회원이시라면 로그인, 처음 방문하셨다면 회원가입을 해주세요.\r\n1. 회원가입          2. 로그인     : ";
            while (is_running) // is_running 이 false가 되면 와일 종료
            {
                if (user_checking ==3)
                {
                    break;
                }
                std::cin >> input_check_needed;
                if (!IsNumericOverall(input_check_needed))
                {
                    std::cout << "회원가입은 1 로그인은 2를 입력해주세요   : ";
                    continue;
                }
                if (IsNumericOverall(input_check_needed))
                {
                    user_checking = stoi(input_check_needed);
                    switch(user_checking)
                    {
                        case 1:
                            {
                                std::cout << "사용자 이름을 입력해주세요. ";
                                std::cin.clear();
                                std::cin.ignore(BUFFER_SIZE, '\n');
                                std::getline(std::cin, user_name);
                                std::cout << "\r\nID를 입력해주세요. ";
                                std::getline(std::cin, user_id);
                                std::cout << "\r\n비밀번호를 입력해주세요. (초기: 1234) ";
                                user_password = "1234";
                                while (!correct_password)
                                {
                                    for (int i = 0; i < 3; i++)
                                    {
                                        input_password = get_masked_input();
                                        if (user_password != input_password)
                                        {
                                            std::cout << "비밀번호를 정확히 입력해 주세요. 초기 비밀번호는 1234 입니다.\r\n";
                                            continue;
                                        }
                                        correct_password = true;
                                        std::cout << "\r\n재설정할 비밀번호를 입력하십시오 : ";
                                        user_password = get_masked_input();
                                        user_password = trim(user_password);
                                        std::cout << "\r\n가입이 완료되었습니다.\r\n";
                                        AddUser(user_name, user_id, user_password);
                                        std::ofstream outFile("User_Info.csv", std::ios::app);
                                        if (outFile.is_open())
                                        {
                                            const auto lastuser = g_user_list.back();
                                            outFile << lastuser.name << ", " << lastuser.id << ", " << lastuser.pw << "\n";
                                            outFile.close();
                                        }
                                        correct_password = true;
                                        is_running = false;
                                        break;                                                                                
                                        }
                                }
                                break;
                            }
                        case 2:
                            {
                                while (user_number == NOT_USER)
                                {
                                    std::cin.ignore(BUFFER_SIZE, '\n');
                                    std::cout << "ID를 입력해주세요 : ";
                                    std::cin >> input_check_needed;
                                    id_for_checking = trim(input_check_needed);
                                    std::cin.ignore(BUFFER_SIZE, '\n');
                                    std::cout << "\r\n비밀번호를 입력해주세요 : ";
                                    std::cin >> input_check_needed;
                                    pw_for_checking = trim(input_check_needed);
                                    user_number = Login(id_for_checking, pw_for_checking, g_user_list);
                                    if (user_number == NOT_USER)
                                    {
                                        std::cout << "\r\nid 혹은 비밀번호가 틀렸습니다. 다시 입력해주세요. \r\n";
                                        continue;
                                    }
                                    std::cout << g_user_list[user_number].name << "님 환영합니다.\r\n";
                                    is_running = false;
                                    break;
                                }
                                break;
                            }
                        default :
                            {
                                std::cout << "1 혹은 2 로 입력해주세요\r\n";
                                continue;
                            }

                    }

                }
            }
        }
        std::cout << "\r\n----------------------------------";
        std::cout << "\r\n환영합니다 숫자로 음식파는 전문점 입니다.";
        std::cout << "\r\n----------------------------------";

        
        major_menu_picked_by_user = 0;
        //주문시작
        while (major_menu_picked_by_user != 6)
        {
            std::cout << "\r\n메뉴입니다." << "\r\n" << "1. 밥  2. 국   3. 반찬 4. 주류 5. 주문종료" << "\r\n" << "원하시는 품목을 선택해주세요. ";
            std::cin >> input_check_needed;
            if (!IsNumericOverall(input_check_needed)) {
                std::cout << "숫자만 입력해주세요.\n";
                continue;
            }
            major_menu_picked_by_user = std::stoi(input_check_needed);

            switch (major_menu_picked_by_user) {
            case 1:
                if (menu_major[0] < 2)
                {
                    menu_major[0] += 1;
                    for (size_t i = 0; i < meal.meal_name.size(); i++)
                        std::cout << meal.meal_name[i] << "     (" << meal.meal_price[i] << "원)        ";
                    specific_menu_picked_by_user = MenuPicker((int)meal.meal_name.size());
                    bag_menu.push_back(meal.meal_name[specific_menu_picked_by_user]);
                    bag_price.push_back(meal.meal_price[specific_menu_picked_by_user]);
                }
                else std::cout << "\r\n밥은 그만!\n";
                break;
            case 2:
                if (menu_major[1] < 3) {
                    menu_major[1] += 1;
                    for (size_t i = 0; i < soup.soup_name.size(); i++) std::cout << soup.soup_name[i] << "      " << soup.soup_price[i] << "        ";
                    specific_menu_picked_by_user = MenuPicker((int)soup.soup_name.size());
                    bag_menu.push_back(soup.soup_name[specific_menu_picked_by_user]);
                    bag_price.push_back(soup.soup_price[specific_menu_picked_by_user]);
                }
                else std::cout << "\r\n국은 그만!\n";
                break;
            case 3:
                menu_major[2] += 1;
                for (size_t i = 0; i < dish.dish_name.size(); i++) std::cout << dish.dish_name[i] << "      " << dish.dish_price[i] << "        ";
                specific_menu_picked_by_user = MenuPicker((int)dish.dish_name.size());
                bag_menu.push_back(dish.dish_name[specific_menu_picked_by_user]);
                bag_price.push_back(dish.dish_price[specific_menu_picked_by_user]);
                break;
            case 4:
                if (isAdult()) {
                    menu_major[3] += 1;
                    for (size_t i = 0; i < drink.drink_name.size(); i++) std::cout << drink.drink_name[i] << "      " << drink.drink_price[i] << "        ";
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
                        bag_menu.clear();
                        bag_price.clear();
                        break;
                    }
                    std::cout << "1 혹은 2로 입력해주세요.\n";
                    continue;
                }
                break;
            case 99:
                while (true)
                {
                    std::cout << "관리자 비밀번호 : "; //초기세팅 0000
                    std::cin >> input_check_needed;
                    if (!IsNumericOverall(input_check_needed))
                    {
                        std::cout << "\r\n다시입력하세요.\r\n";
                        continue;
                    }
                    if (input_check_needed == "0000")
                    {
                        std::cout << "매출은" << sales << "원 입니다.\r\n";
                        return 0;
                    }
                }
            default:
                std::cout << "1과 5 사이의 숫자로 입력해주세요. \r";
                major_menu_picked_by_user = 0;
                break;
            }
        }

        // 주문에 따른 할인적용
        discount_rate = 0.0;
        if (menu_major[2] >= 9)
        {
            /*std::cout << "확인용 반찬개수: " << menu_major[2] << "\r\n";*/
            std::cout << "반찬을 9가지 이상 주문하셔서 20%를 할인 예약합니다.\r\n";
            discount_rate += 0.2;
        }

        // 결제시작
        while (!is_payed_succesfully)
        {
            std::cin.ignore(BUFFER_SIZE, '\n');
            std::cout << "결제수단이 현금이시면 1, 카드시면 2 를 눌러주세요.";
            payment = 0;
            std::cin >> input_check_needed;
            if (!IsNumericOverall(input_check_needed))
            {
                std::cout << "1 혹은 2의 숫자로만 입력해 주세요! \r\n";
                continue;
            }
            payment = std::stoi(input_check_needed);
            if (payment == 1) //현금결제 추가할인
            {
                std::cout << "현금 결제 10% 추가 할인이 예약되었습니다.\n";
                discount_rate += 0.1;
            }

            final_price = (int)(total_price * (1.0 - discount_rate));
            std::cout << "최종 결제 금액은 " << final_price << "원 입니다. (총 할인: " << (int)(discount_rate * 100) << "%)\n";
            if (payment == 1) // 현금
            {
                std::cin.ignore(BUFFER_SIZE, '\n');
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
                        if (payed_bill == 0) //현금결제취소시 할인 번복
                        {
                            discount_rate -= 0.1;
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

        //초기화부분
        is_running = true;
        is_payed_succesfully = false;
        user_number = -1;
        sales += final_price; // 매출기록
        Update_Total_Sales(sales);
        menu_major.fill(0);
        bag_menu.clear();
        bag_price.clear();
        total_price = 0;
        std::cin.ignore(BUFFER_SIZE, '\n');

        
        
    }
}
