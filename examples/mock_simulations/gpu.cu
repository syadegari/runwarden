#include "common.hpp"
#include <cuda_runtime.h>
#include <utility>

inline void check(cudaError_t code) {
    if (code != cudaSuccess) throw std::runtime_error(cudaGetErrorString(code));
}

struct DeviceArray {
    float* data = nullptr;
    DeviceArray() { check(cudaMalloc(reinterpret_cast<void**>(&data), cells * sizeof(float))); }
    ~DeviceArray() { if (data) cudaFree(data); }
    DeviceArray(const DeviceArray&) = delete;
    DeviceArray& operator=(const DeviceArray&) = delete;
};

__global__ void heat_step(const float* x, float* next) {
    int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i < cells) {
        int left = (i + cells - 1) % cells;
        int right = (i + 1) % cells;
        next[i] = x[i] + 0.2f * (x[left] - 2.0f * x[i] + x[right]);
    }
}

__global__ void particle_step(float* x, float* velocity) {
    int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i < cells) {
        velocity[i] -= dt * x[i];
        x[i] += dt * velocity[i];
    }
}

int main(int argc, char** argv) {
    try {
        const auto opt = options(argc, argv, false);
        const char* visible = std::getenv("CUDA_VISIBLE_DEVICES");
        if (!visible || std::string(visible) != "0")
            throw std::runtime_error("Submit through rmgr with --gpu (CUDA_VISIBLE_DEVICES must be 0)");
        check(cudaSetDevice(0));
        // Sleep while waiting for the GPU, rather than burning the reserved CPU.
        check(cudaSetDeviceFlags(cudaDeviceScheduleBlockingSync));
        cudaDeviceProp properties{};
        check(cudaGetDeviceProperties(&properties, 0));
        std::vector<float> x(cells), velocity(cells, 0);
        initialize(x, opt.heat);
        DeviceArray a, b, v;
        float* current = a.data;
        float* next = b.data;
        const auto bytes = cells * sizeof(float);
        check(cudaMemcpy(current, x.data(), bytes, cudaMemcpyHostToDevice));
        check(cudaMemset(v.data, 0, bytes));
        std::printf("started backend=gpu mode=%s device=%s cells=%d device_memory_mib=12 duration=%.2fs\n",
                    argv[1], properties.name, cells, opt.seconds);
        std::fflush(stdout);
        auto started = Clock::now();
        double next_log = 1;
        long long steps = 0;
        do {
            for (int batch = 0; batch < 100; ++batch) {
                if (opt.heat) {
                    heat_step<<<(cells + 255) / 256, 256>>>(current, next);
                    std::swap(current, next);
                } else {
                    particle_step<<<(cells + 255) / 256, 256>>>(current, v.data);
                }
                check(cudaGetLastError());
            }
            check(cudaDeviceSynchronize());
            steps += 100;
            if (elapsed(started) >= next_log) {
                float sample;
                check(cudaMemcpy(&sample, current + 64, sizeof(float), cudaMemcpyDeviceToHost));
                progress(opt, started, steps, sample);
                next_log = elapsed(started) + 1;
            }
        } while (elapsed(started) < opt.seconds);
        check(cudaMemcpy(x.data(), current, bytes, cudaMemcpyDeviceToHost));
        check(cudaMemcpy(velocity.data(), v.data, bytes, cudaMemcpyDeviceToHost));
        finish(opt, x, velocity, started, steps);
        return 0;
    } catch (const std::exception& exc) {
        std::fprintf(stderr, "mock_gpu: %s\n", exc.what());
        return 1;
    }
}
