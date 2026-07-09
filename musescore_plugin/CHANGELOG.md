# MuseScore WebSocket Plugin - 修改记录

## 来源

基于 [ghchen99/mcp-musescore](https://github.com/ghchen99/mcp-musescore) 的 `musescore-mcp-websocket.qml` 插件修改。

原始文件：https://github.com/ghchen99/mcp-musescore/blob/main/musescore-mcp-websocket.qml

## 安装位置

```
~/Documents/MuseScore4/Plugins/musescore-mcp-websocket.qml
```

## 修改内容

### 1. 修复 `addNote` 中 `elements.some()` 类型错误

**问题**：`selectionState.elements` 在不同调用路径下有两种格式：
- 数组格式：`[{name: "Rest", ...}]`（`initCursorState` 产生）
- 对象格式：`{staff0: [{name: "Rest", ...}], staff1: [...]}`（`syncStateToSelection` 产生）

原始代码在第 716 行直接调用 `.some()`，假设 `elements` 一定是数组，在 `syncStateToSelection` 之后会报错：
```
TypeError: Property 'some' of object [object Object] is not a function
```

**修改位置**：`addNote` 函数内部（约第 716 行）

**原始代码**：
```javascript
// Check if current position has a rest
var hasRest = selectionState.elements.some(function(element) { 
    return element.name === "Rest"; 
});
```

**修改后**：
```javascript
// Check if current position has a rest
var hasRest = false;
var elems = selectionState.elements;
if (Array.isArray(elems)) {
    hasRest = elems.some(function(element) { return element.name === "Rest"; });
} else if (typeof elems === "object") {
    var keys = Object.keys(elems);
    for (var i = 0; i < keys.length && !hasRest; i++) {
        var arr = elems[keys[i]];
        if (Array.isArray(arr)) {
            hasRest = arr.some(function(element) { return element.name === "Rest"; });
        }
    }
}
```

---

### 2. 新增 `clearScore` 命令

**问题**：`undo` 有历史栈上限，无法彻底回退大量操作。需要一个一步到位清空所有音符的命令。

**修改位置 1**：`processCommand` 的 switch 语句中注册新命令

```javascript
case "ping":                    return "pong";
case "undo":                    return undo();
case "clearScore":             return clearScore();  // ← 新增
case "goToBeginningOfScore":    return goToBeginningOfScore();
```

**修改位置 2**：在 `undo()` 函数之后添加 `clearScore` 实现

```javascript
function clearScore() {
    if (!curScore) return { error: "No score open" };
    curScore.startCmd();
    cmd("select-all");
    cmd("delete");
    curScore.endCmd();
    return { success: true, message: "Score cleared" };
}
```

**工作原理**：调用 MuseScore 内置的 `select-all` 命令全选所有内容，然后 `delete` 删除。用 `startCmd/endCmd` 包装使其成为一个可撤销的原子操作。

---

## 注意事项

- 修改插件后需要**完全退出 MuseScore 再重新打开**，仅 "Reload plugins" 不会终止已运行的旧插件 WebSocket 实例。
- 插件需要在**有乐谱打开的状态下**才能运行（Plugins 菜单 → 点击插件名称）。
- 每次打开新文件后需要重新运行插件。
