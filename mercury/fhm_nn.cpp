// See fhm_nn.h. Built with ExecuTorch when FHM_EXECUTORCH is defined (mercury/build.sh --executorch).
#include "fhm_nn.h"

#include <cstdlib>
#include <cstring>

#ifdef FHM_EXECUTORCH
#include "hexecutor_backend.hpp"

#include <executorch/extension/data_loader/file_data_loader.h>
#include <executorch/extension/memory_allocator/malloc_memory_allocator.h>
#include <executorch/extension/tensor/tensor_ptr_maker.h>
#include <executorch/runtime/core/hierarchical_allocator.h>
#include <executorch/runtime/executor/memory_manager.h>
#include <executorch/runtime/executor/method.h>
#include <executorch/runtime/executor/program.h>
#include <executorch/runtime/platform/runtime.h>

#include <memory>
#include <mutex>
#include <optional>
#include <vector>
#endif

namespace fhm_nn {

namespace {
enum class Choice { onnx, executorch, invalid };

Choice choice() {
    const char *v = std::getenv("FHL_NN_BACKEND");
    if (!v || !*v || std::strcmp(v, "onnx") == 0)
        return Choice::onnx;
    if (std::strcmp(v, "executorch") == 0)
        return Choice::executorch;
    return Choice::invalid;
}
} // namespace

bool use_executorch() { return choice() == Choice::executorch; }

const char *backend_error() {
    return choice() == Choice::invalid ? "FHL_NN_BACKEND must be onnx or executorch" : nullptr;
}

#ifdef FHM_EXECUTORCH

using executorch::aten::ScalarType;
using executorch::aten::SizesType;
using executorch::extension::FileDataLoader;
using executorch::extension::MallocMemoryAllocator;
using executorch::runtime::Error;
using executorch::runtime::EValue;
using executorch::runtime::HierarchicalAllocator;
using executorch::runtime::MemoryManager;
using executorch::runtime::Method;
using executorch::runtime::Program;
using executorch::runtime::Span;

struct Model {
    std::optional<FileDataLoader> loader;
    std::optional<Program> program;
    std::vector<std::vector<uint8_t>> planned_bytes;
    std::vector<Span<uint8_t>> planned_spans;
    std::unique_ptr<HierarchicalAllocator> planned;
    MallocMemoryAllocator method_alloc, temp_alloc;
    std::unique_ptr<MemoryManager> memory;
    std::optional<Method> method;
    std::vector<std::vector<float>> input_data; // the method's input tensors point here
    std::vector<executorch::extension::TensorPtr> input_tensors;
    std::vector<size_t> output_numel;
};

namespace {
bool fail(std::string *error, const std::string &what, Error e = Error::Ok) {
    if (error) {
        *error = what;
        if (e != Error::Ok)
            *error += " (ExecuTorch error " + std::to_string(int(e)) + ")";
    }
    return false;
}

std::once_flag g_init;
} // namespace

Model *load(const std::string &path, std::string *error) {
    std::call_once(g_init, [] { executorch::runtime::runtime_init(); });
    if (hexecutor_et::ensure_registered() != Error::Ok) {
        fail(error, "HexecutorBackend is not registered");
        return nullptr;
    }
    auto m = std::make_unique<Model>();
    auto loader = FileDataLoader::from(path.c_str());
    if (!loader.ok()) {
        fail(error, "cannot open " + path, loader.error());
        return nullptr;
    }
    m->loader.emplace(std::move(loader.get()));
    auto program = Program::load(&*m->loader);
    if (!program.ok()) {
        fail(error, "cannot load " + path, program.error());
        return nullptr;
    }
    m->program.emplace(std::move(program.get()));
    auto meta = m->program->method_meta("forward");
    if (!meta.ok()) {
        fail(error, path + " has no forward method", meta.error());
        return nullptr;
    }
    const size_t n_planned = meta->num_memory_planned_buffers();
    m->planned_bytes.reserve(n_planned);
    for (size_t i = 0; i < n_planned; i++) {
        auto size = meta->memory_planned_buffer_size(i);
        if (!size.ok()) {
            fail(error, "bad planned buffer size in " + path, size.error());
            return nullptr;
        }
        m->planned_bytes.emplace_back(size_t(size.get()));
        m->planned_spans.emplace_back(m->planned_bytes.back().data(), m->planned_bytes.back().size());
    }
    m->planned = std::make_unique<HierarchicalAllocator>(
        Span<Span<uint8_t>>(m->planned_spans.data(), m->planned_spans.size()));
    m->memory = std::make_unique<MemoryManager>(&m->method_alloc, m->planned.get(), &m->temp_alloc);
    // Delegate init happens here: the backend opens (or joins) the process's DSP session.
    auto method = m->program->load_method("forward", m->memory.get());
    if (!method.ok()) {
        fail(error, "cannot load forward of " + path + " (check the HEXECUTOR_* artifacts and cdsp_mem)",
             method.error());
        return nullptr;
    }
    m->method.emplace(std::move(method.get()));

    const size_t n_in = meta->num_inputs();
    m->input_data.reserve(n_in);
    for (size_t i = 0; i < n_in; i++) {
        auto info = meta->input_tensor_meta(i);
        if (!info.ok() || info->scalar_type() != ScalarType::Float) {
            fail(error, path + ": every input must be float32");
            return nullptr;
        }
        std::vector<SizesType> sizes(info->sizes().begin(), info->sizes().end());
        size_t n = 1;
        for (SizesType s : sizes)
            n *= size_t(s);
        m->input_data.emplace_back(n, 0.0f);
        m->input_tensors.push_back(
            executorch::extension::from_blob(m->input_data.back().data(), sizes, ScalarType::Float));
    }
    for (size_t i = 0; i < meta->num_outputs(); i++) {
        auto info = meta->output_tensor_meta(i);
        if (!info.ok() || info->scalar_type() != ScalarType::Float) {
            fail(error, path + ": every output must be float32");
            return nullptr;
        }
        size_t n = 1;
        for (SizesType s : info->sizes())
            n *= size_t(s);
        m->output_numel.push_back(n);
    }
    return m.release();
}

void destroy(Model *m) { delete m; }

size_t num_inputs(const Model *m) { return m->input_data.size(); }
size_t num_outputs(const Model *m) { return m->output_numel.size(); }
size_t input_elements(const Model *m, size_t i) { return m->input_data[i].size(); }
size_t output_elements(const Model *m, size_t i) { return m->output_numel[i]; }

bool run(Model *m, const float *const *inputs, const float **outputs, std::string *error) {
    // set_input copies into the method's planned input memory, so it runs every time.
    for (size_t i = 0; i < m->input_data.size(); i++) {
        std::memcpy(m->input_data[i].data(), inputs[i], m->input_data[i].size() * sizeof(float));
        const Error e = m->method->set_input(EValue(*m->input_tensors[i]), i);
        if (e != Error::Ok)
            return fail(error, "set_input " + std::to_string(i) + " failed", e);
    }
    const Error e = m->method->execute();
    if (e != Error::Ok)
        return fail(error, "forward failed", e);
    for (size_t i = 0; i < m->output_numel.size(); i++) {
        const EValue &out = m->method->get_output(i);
        if (!out.isTensor() || size_t(out.toTensor().numel()) != m->output_numel[i])
            return fail(error, "unexpected output " + std::to_string(i));
        outputs[i] = out.toTensor().const_data_ptr<float>();
    }
    return true;
}

#else // !FHM_EXECUTORCH

struct Model {};

Model *load(const std::string &, std::string *error) {
    if (error)
        *error = "this Mercury library was built without ExecuTorch (mercury/build.sh --executorch)";
    return nullptr;
}
void destroy(Model *m) { delete m; }
size_t num_inputs(const Model *) { return 0; }
size_t num_outputs(const Model *) { return 0; }
size_t input_elements(const Model *, size_t) { return 0; }
size_t output_elements(const Model *, size_t) { return 0; }
bool run(Model *, const float *const *, const float **, std::string *error) {
    if (error)
        *error = "built without ExecuTorch";
    return false;
}

#endif

} // namespace fhm_nn
