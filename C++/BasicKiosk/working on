#include <iostream>
#include <string> //문자열사용을 위한 헤더파일
#include <sstream>
#include <array> //배열사용을 위한 헤더파일
#include <vector> //벡터사용을 위한 헤더파일
#include <regex> //정규표현식을 위한 헤더파일
#include <fstream> //파일에 입출력을 위한 헤더파일
#include <set> //set 쓰기위한 헤더파일
#include <ctime> //시간
#include <iomanip> //시간을 이쁘게쓰기

#include <termios.h> // 리눅스 터미널 설정용
#include <unistd.h>  // STDIN_FILENO용

#include <iostream>
#include <termios.h>
#include <unistd.h>
#include <string>

enum LoginStatus
{
    SUCCESS,
    FAILED,
    CHOOSE_AGAIN
};


struct Menu
{
    std::string category;
    std::string menu_name;
    int price;
};



void LoadMenu(const std::string& file_name, std::vector<Menu>& menu_list)
{
    std::ifstream file(file_name);
    std::string line;

    if(!file.is_open())
    {
        std::cerr << "Error!\r\n";
        return;
    }
    //파일 한줄씩 읽기
    while (std::getline(file, line))
    {
        std::stringstream ss(line);
        std::string category, menu_name, priceStr;
        //쉼표 기준으로 데이터를 구분해서 받기
        std::getline(ss, category, ',');
        std::getline(ss, menu_name, ',');
        std::getline(ss, priceStr, ',');

        //구분한 데이터를 price는 int로 변환해서 객체(클래스)생성
        int price = std::stoi(priceStr);
        menu_list.push_back({category, menu_name, price});
    }
    
    file.close();
}

struct TerminalRestorer 
{
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
        }
        else {
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
bool is_numeric(const std::string& input) // 정수인지 확인하고, true false로 반환.
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
std::string trim(const std::string& s) {
    size_t first = s.find_first_not_of(" \t\n\r\f\v");
    if (first == std::string::npos) return ""; // 공백만 있는 경우
    size_t last = s.find_last_not_of(" \t\n\r\f\v");
    return s.substr(first, (last - first + 1));
}
LoginStatus NewAccount(const std::string& user_file)
{
    std::string name, new_id, new_pw;
    std::cout << "이름을 입력해주세요. \r\n";
    std::getline(std::cin, name);
    bool available_id = false;
    while(!available_id)
    {
        std::cout << "사용하실 id를 입력해 주세요. 이미 가입셨다면 0 을 입력해주세요.\r\n";
        std::getline(std::cin, new_id);
        if(new_id == "0")
        return CHOOSE_AGAIN;
        new_id = trim(new_id);
        std::ifstream file(user_file);
        while (!file.is_open())
        {
            std::cout << "Error!\r\n";
            return FAILED;
        }
        std::string checking_line;
        while(std::getline(file, checking_line))
        {
            std::stringstream ss(checking_line);
            std::string name, id, pw;
            // CSV 구조가 "이름,ID,PW"라고 가정할 때
            std::getline(ss, name, ',');
            name = trim(name);
            std::getline(ss, id, ',');
            id = trim(id);
            std::getline(ss, pw, '\n'); // 마지막 필드는 줄바꿈까지 읽음
            std::cout << "읽은 id : " << id;
            if (new_id == id)
            {
                std::cout << "이미 존재하는 id입니다. 다른 id를 입력하셔야합니다.\r\n";
                break;
            }
            available_id = true;
            break;
        }
        if(!available_id)
        continue;
        file.close();
        std::cout << "사용하실 수 있는 아이디입니다.\r\n";
        std::cout << "사용하실 비밀번호를 입력해 주십시오.\r\n";
        std::getline(std::cin, new_pw);
        new_pw = trim(new_pw);
        std::ofstream openFile_for_add_user(user_file, std::ios::app);
        openFile_for_add_user << name << ", " << new_id << ", " << new_pw << "\n";
        openFile_for_add_user.close();
        std::cout << "가입이 완료되었습니다! 로그인해주십시오!\r\n";
        return CHOOSE_AGAIN;
    }
    return CHOOSE_AGAIN;
}

int Login(const std::string& user_file) // id, pw, 유저정보파일입력
{
    std::string input_id, input_pw;
    std::cout << "id를 입력해주세요";
    std::getline(std::cin, input_id);
    input_id = trim(input_id);
    std::cout << "비밀번호를 입력해주세요";
    std::getline(std::cin, input_pw);
    input_pw = trim(input_pw);    
    std::ifstream file(user_file); // 파일열기
    if(!file.is_open())
    {
        std::cout << "Error!\r\n"; // 파일오류로 로그인 불가
        return FAILED;
    }
    std::string checking_line;
    while(std::getline(file, checking_line))
    {
        std::stringstream ss(checking_line);
        std::string name, id, pw;
        std::getline(ss, name, ','); 
        std::getline(ss, id, ',');
        std::getline(ss, pw, '\n'); // 마지막 필드는 줄바꿈까지 읽음
        name = trim(name);
        id = trim(id);
        pw = trim(pw);
        if(id == input_id && pw == input_pw)
        {
            std::cout << name << "님 환영합니다\r\n";
            return SUCCESS;
        }
    }
    std::cout << "일치하는 id와 비밀번호가 없습니다. 다시 선택해주세요 \r\n";
    return CHOOSE_AGAIN;
}

int main()
{
    const int Success = 0;
    const int Failed = 1;
    const int ChooseAgain = 2;
    std::vector <Menu> menu_list;
    LoadMenu("Menu.csv", menu_list);
    std::set<std::string> category_set;
    for(const auto& major_menu : menu_list)
    {
        category_set.insert(major_menu.category); // 카테고리 안의 메뉴가 몇개있든, 하나로 취급. 임시로 major_menu 이름을 붙임.
    }
    std::vector<std::string> category_list(category_set.begin(), category_set.end());
    std::string input_check_needed; // 모든 입력은 체크하기위해 inputcheckneeded 문자열로 받음.
    int logincheck = ChooseAgain;
    int whom;
    std::string user_name, input_id, input_pw;
    while (logincheck == ChooseAgain)
    {
        std::cout << "처음 방문하셨다면 회원가입을 위해 1 을, 기존회원이시면 로그인을 위해 2 를 입력해주세요.\r\n";
        std::getline(std::cin, input_check_needed);
        if (!IsNumericOverall(input_check_needed))
        {
            std::cout << "잘못된 입력입니다. 1 혹은 2로 입력해주세요.";
            continue;
        }
        int whom = std::stoi(input_check_needed);
        switch (whom)
        {
        case 1:
        {
            logincheck =  NewAccount("User_Info.csv");
            continue;
        }
        case 2:
        {
            logincheck = Login("User_Info.csv");
            break;
        }
        default:
            std::cout << "1 혹은 2의 숫자로 입력해주세요\r\n";
        }
        if (logincheck == Success)
        {
            std::cout << "로그인성공\r\n";
            break;
        }
    }
    int total_price = 0;
    bool isPaidWell = false;
    while (!isPaidWell)
    {
        std::cout << "결제 수단을 선택해 주십시오. 1. 현금 / 2. 카드\r\n현금을 선택하시면 10% 할인율이 추가로 적용됩니다.\r\n";
        std::getline(std::cin, input_check_needed);
        if(!is_numeric)
        {
            std::cout << "숫자 1 혹은 2 로 입력해주세요.\r\n";
            continue;
        }
        if(!IsNumericOverall)
        {
            std::cout << "1 혹은 2의 숫자로 입력해주세요.\r\n";
            continue;
        }
        std::cout << "------------------------\r\n-----환영합니다 000입니다.\r\n------------------------------";
        int major_menu_picker = 0;
        const int menu_meal = 1;
        const int menu_soup = 2;
        const int menu_dish = 3;
        const int menu_drink = 4;
        const int menu_finish = 5;
        while (major_menu_picker != menu_finish)
        {
            std::cout<< "식사는 1번 국은 2번 반찬은 3번 주류는 4번 메뉴선정이 끝나셨다면 5번을 눌러주세요 : ";
            std:getline(std::cin, input_check_needed);
            if (!is_numeric(input_check_needed))
            {
                std::cout << "\r\n숫자로만 입력해주세요!\r\n";
            }
            if (!IsNumericOverall(input_check_needed))
            {
                std::cout<<"1~5까지의 숫자로만 입력해주세요! \r\n";
            }
            major_menu_picker = stoi(input_check_needed) - 1;
            switch (major_menu_picker)
            {
            case 1:
                for (int i = 0; i < menu_meal.size(); i++)
                {
                    std::cout << i+1 <<"." << menu_name
                }
                
                break;
            
            default:
                break;
            }
            
            
        }
        

        
    }
    return 0;
}
