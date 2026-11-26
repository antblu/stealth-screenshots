#include <iostream>

int run_core_tests();

int main() {
    const int failures = run_core_tests();
    if (failures == 0) {
        std::cout << "All stealth_core_tests passed.\n";
        return 0;
    }
    std::cerr << failures << " test(s) failed.\n";
    return 1;
}
