# Stata C Plugins Prompt Fixtures

1. Plan a minimal Stata plugin that reads a numeric variable, computes a transformed value in C, and exposes it through an ado wrapper.
2. Explain how to compile a C++ plugin for Stata on macOS and why `extern "C"` is required for `stata_call`.
3. Outline a validation workflow for porting an existing Python statistical routine into a Stata plugin without silently changing results.
