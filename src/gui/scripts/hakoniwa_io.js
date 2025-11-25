// scripts/hakoniwa_io.js
(function(global) {

  async function loadJsonFromFileInput(inputId) {
    const fi = document.getElementById(inputId);
    if (!fi || !fi.files || fi.files.length === 0) {
      throw new Error("JSONファイルが選択されていません");
    }

    const file = fi.files[0];
    const text = await file.text();

    try {
      return JSON.parse(text);
    } catch(e) {
      throw new Error("JSON解析に失敗しました: " + e.message);
    }
  }

  function downloadJson(obj, filename) {
    const text = JSON.stringify(obj, null, 2);
    const blob = new Blob([text], { type: "application/json" });
    const url = URL.createObjectURL(blob);

    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    a.click();

    URL.revokeObjectURL(url);
  }

  global.HakoniwaIO = {
    loadJsonFromFileInput,
    downloadJson,
  };

})(window);
