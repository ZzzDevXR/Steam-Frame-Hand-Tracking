// Mercury's neural networks through ExecuTorch (HexecutorBackend on the Frame CDSP).
//
// Monado's hg_model.cpp (patched by monado-executorch.patch) calls this when FHL_NN_BACKEND=executorch.
// One Model is one loaded .pte "forward" method. A Model is not reentrant: Mercury uses each of its model
// states from one thread at a time. Different Models may run concurrently; the backend serializes the DSP.
#pragma once

#include <cstddef>
#include <string>

namespace fhm_nn {

struct Model;

// True when FHL_NN_BACKEND=executorch. Any other value except unset or "onnx" is an error reported by
// backend_error(), so a typo never silently selects ONNX Runtime.
bool use_executorch();
const char *backend_error();

// Loads `path` (a .pte whose forward takes and returns float32 tensors). Returns null and sets *error on
// failure, including when this library was built without ExecuTorch.
Model *load(const std::string &path, std::string *error);
void destroy(Model *);

size_t num_inputs(const Model *);
size_t num_outputs(const Model *);
size_t input_elements(const Model *, size_t index);
size_t output_elements(const Model *, size_t index);

// Runs forward on `inputs` (num_inputs pointers, each input_elements floats, read during the call).
// On success `outputs` receives num_outputs pointers owned by the model, valid until the next run or destroy.
bool run(Model *, const float *const *inputs, const float **outputs, std::string *error);

} // namespace fhm_nn
