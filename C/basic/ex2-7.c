#define _CRT_SECURE_NO_WARNINGS 
#include <stdio.h>
int main()
{
	printf("%c\n", 'A');	//문자 - 한글자
	printf("%s\n", "A");	//문자열 - "A\0" <--두분자. \0 = 널문자.
	printf("%c은 %s입니다.\n", '1', "first");

	printf("%c %d %c %d\n", '0', '0', '1', '1');
	//유니코드(utf-8), ASCII(아스키코드)
	printf(" %c % c % c % c % c\n", 'h', 'e', 'l', 'l', 'o');
	printf("%s\n", "hello");
	return 0;
}
