#include "common.hpp"
#include <algorithm>
#include <omp.h>
#include <sched.h>

int main(int argc, char** argv) {
    try {
        const auto opt = options(argc, argv, true);
        cpu_set_t allowed;
        CPU_ZERO(&allowed);
        if (sched_getaffinity(0, sizeof(allowed), &allowed) != 0)
            throw std::runtime_error("Cannot read CPU affinity");
        if (CPU_COUNT(&allowed) < opt.threads)
            throw std::runtime_error("Fewer CPUs available than requested threads; submit with matching --cores");
        omp_set_dynamic(0);
        omp_set_num_threads(opt.threads);
        int actual_threads = 0;
        #pragma omp parallel
        {
            #pragma omp single
            actual_threads = omp_get_num_threads();
        }
        if (actual_threads != opt.threads)
            throw std::runtime_error("OpenMP did not create the requested thread team");
        std::vector<float> x(cells), next(cells), velocity(cells, 0);
        initialize(x, opt.heat);
        std::printf("started backend=cpu mode=%s threads=%d available_cpus=%d cells=%d duration=%.2fs\n",
                    argv[1], actual_threads, CPU_COUNT(&allowed), cells, opt.seconds);
        std::fflush(stdout);
        auto started = Clock::now();
        double next_log = 1;
        long long steps = 0;
        do {
            if (opt.heat) {
                #pragma omp parallel for schedule(static)
                for (int i = 0; i < cells; ++i) {
                    int left = (i + cells - 1) % cells;
                    int right = (i + 1) % cells;
                    next[i] = x[i] + 0.2f * (x[left] - 2.0f * x[i] + x[right]);
                }
                std::swap(x, next);
            } else {
                #pragma omp parallel for schedule(static)
                for (int i = 0; i < cells; ++i) {
                    velocity[i] -= dt * x[i];
                    x[i] += dt * velocity[i];
                }
            }
            ++steps;
            if (elapsed(started) >= next_log) {
                progress(opt, started, steps, x[64]);
                next_log = elapsed(started) + 1;
            }
        } while (elapsed(started) < opt.seconds);
        finish(opt, x, velocity, started, steps);
        return 0;
    } catch (const std::exception& exc) {
        std::fprintf(stderr, "mock_cpu: %s\n", exc.what());
        return 1;
    }
}
