"""Verified official model revisions and unquantized artifact hashes.

The bbox export removes masks only for digital, rectangular-page layouts.
"""

MODEL_LOCK = {
    "layout": {
        "repo": "PaddlePaddle/PP-DocLayoutV3",
        "revision": "7b48a7566925fa464281f930c58eee04fe2c862a",
        "files": {
            "inference.json": "2b68367c5b312a03de5a6e1642c597c8f95165a7e40cd59c6700cf4a5042f4fd",
            "inference.pdiparams": "70bd316b0582769ec968829fd1feb1a6a58b7c941b938327e551b6b12b45c137",
            "inference.yml": "506fcfac13b3b546ae40d7886b44126420f392adb694e3f8bb6a6286a1f90fdc",
        },
    },
    "vlm": {
        "repo": "PaddlePaddle/PaddleOCR-VL-1.6",
        "revision": "c5630abae1d940eafe0697512a0325494b02ab42",
        "files": {
            "added_tokens.json": "f59f889088e0fe21c523e7cf121bb6dca3b0bb148cb7159fbb4572c74dfc5644",
            "chat_template.jinja": "2f27812dab7f333e471884e0c803d807f11953d5453140dfb1aaba234f872bc8",
            "config.json": "ce7f4565f8b1db78532ad5d1b9ebe55c2139d49bd4cb04778b580a08a598f171",
            "configuration_paddleocr_vl.py": "753dd93654c3a9c8c85a3eaee1e3092dd12591b0f2dce0305e1abfb7a41ff160",
            "generation_config.json": "a6701d78ab3b4d972307cdec3b69d4c13f46e0d5140514f50ab7d84259324b94",
            "image_processing_paddleocr_vl.py": "a4fa521b9cb16e207f94b7f2d16427771776dfc634420d319fc4916ee58049ec",
            "inference.yml": "1587aece2d6442366efce34161d5f7d5f67f09ebfbf043168e5fade892c2780e",
            "model.safetensors": "85a479d506a11e724e7285d395c551be69f41dbc16b6342d3cacfb189aed71db",
            "modeling_paddleocr_vl.py": "c5013dff57ca8b87dc1de64d0fd839a44313de09d230a4fb2d08289d2cad5111",
            "preprocessor_config.json": "111872ab1e8bb7fd040ac5087bfced7ab8f011f02139b088cba294964c3b1d0e",
            "processing_paddleocr_vl.py": "e29cb1e5f275f2bd3ce051bd5c9983a33894e693b2823a0e13d4c07c8c4f9e13",
            "processor_config.json": "1568858960a9760c54431dae693a6152e601ff55cdf6d2eab97a4a99958faea0",
            "special_tokens_map.json": "d3a125c03103deb2acaf7730791bdbbf196f620e5a2213b664511ff9b4b25bab",
            "tokenizer.json": "c8a215a59183d0d0781adc33bacd3ce6162716f7fd568fb30234a74d69803a7d",
            "tokenizer.model": "34ef7db83df785924fb83d7b887b6e822a031c56e15cff40aaf9b982988180df",
            "tokenizer_config.json": "1f979337347cc0cb72a6282d8a23ed183539aa81a87a906f022aee2bab83c7c5",
        },
    },
    "onnx": {
        "repo": "PaddlePaddle/PP-DocLayoutV3_onnx",
        "revision": "46bbdf188bb0a772c08aed74882ce7e51a8f1ea6",
        "files": {
            "inference.onnx": "45bf71750b00739a41fc209f132eb104a4d6b5bb29483c9078164d8b87cf28ba",
            "inference_bbox.onnx": "fe3bc78476c982401caf389a8e8e928cb94cc0dbb89be73a363838f19fcaf271",
        },
    },
}
