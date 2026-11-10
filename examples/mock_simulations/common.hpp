#pragma once

#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <stdexcept>
#include <string>
#include <vector>

using Clock = std::chrono::steady_clock;
constexpr int cells = 1 << 20;
constexpr float dt = 0.01f;

struct Options {
    bool heat;
    double seconds;
    int threads;
};

inline Options options(int argc, char** argv, bool cpu) {
    if (argc < 2 || argc > (cpu ? 4 : 3))
        throw std::runtime_error(cpu ? "Usage: mock_cpu heat|particles [seconds=30] [threads=16]"
                                     : "Usage: mock_gpu heat|particles [seconds=30]");
    std::string mode(argv[1]);
    if (mode != "heat" && mode != "particles")
        throw std::runtime_error("Mode must be heat or particles");
    char* end = nullptr;
    double seconds = argc > 2 ? std::strtod(argv[2], &end) : 30;
    if (!std::isfinite(seconds) || seconds <= 0 || seconds > 3600 ||
        (argc > 2 && (end == argv[2] || *end)))
        throw std::runtime_error("Seconds must be in (0, 3600]");
    long threads = cpu && argc > 3 ? std::strtol(argv[3], &end, 10) : 16;
    if (threads < 1 || threads > 256 || (cpu && argc > 3 && (end == argv[3] || *end)))
        throw std::runtime_error("Threads must be an integer in [1, 256]");
    return {mode == "heat", seconds, static_cast<int>(threads)};
}

inline double elapsed(Clock::time_point started) {
    return std::chrono::duration<double>(Clock::now() - started).count();
}

inline void initialize(std::vector<float>& x, bool heat) {
    for (int i = 0; i < cells; ++i) {
        // Repeated short waves make diffusion visible within a short demo.
        float wave = std::sin(6.283185307179586 * (i % 256) / 256);
        x[i] = heat ? 1.0f + 0.5f * wave : wave;
    }
}

inline void progress(const Options& opt, Clock::time_point started, long long steps, float sample) {
    std::printf("progress mode=%s elapsed=%.2fs steps=%lld sample=%.6f\n",
                opt.heat ? "heat" : "particles", elapsed(started), steps, sample);
    std::fflush(stdout);
}

inline void finish(const Options& opt, const std::vector<float>& x,
                   const std::vector<float>& velocity, Clock::time_point started, long long steps) {
    double sum = 0;
    double energy = 0;
    for (int i = 0; i < cells; ++i) {
        if (!std::isfinite(x[i]) || !std::isfinite(velocity[i]))
            throw std::runtime_error("Simulation produced non-finite values");
        if (opt.heat && (x[i] < 0.499f || x[i] > 1.501f))
            throw std::runtime_error("Heat solution exceeded initial temperature bounds");
        sum += x[i];
        energy += 0.5 * (double(x[i]) * x[i] + double(velocity[i]) * velocity[i]);
    }
    // Periodic diffusion conserves mean temperature. Harmonic particles should
    // retain their initial mean energy (0.25), within integrator error.
    if (opt.heat ? std::abs(sum / cells - 1.0) > 0.001 : std::abs(energy / cells - 0.25) > 0.005)
        throw std::runtime_error("Simulation conservation check failed");
    std::printf("finished mode=%s elapsed=%.2fs steps=%lld mean=%.8f mean_energy=%.8f checks=passed\n",
                opt.heat ? "heat" : "particles", elapsed(started), steps, sum / cells, energy / cells);
}
