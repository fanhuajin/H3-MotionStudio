import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { stripTypeScriptTypes } from "node:module";
import { test } from "node:test";
import { runInNewContext } from "node:vm";

const source = readFileSync(new URL("../src/BatchRoute.tsx", import.meta.url), "utf8");
const functions = ["batchStatusLabel", "itemZoneStatus", "prepPhase"].map((name) => {
  const match = source.match(new RegExp(`^function ${name}\\([^]*?^}`, "m"));
  assert.ok(match, `Missing ${name}`);
  return match[0];
}).join("\n");
const context = {};
runInNewContext(stripTypeScriptTypes(functions), context);

test("background covers do not override the video or preparation status", () => {
  for (const coverStage of ["bilibili", "douyin"]) {
    for (const [status, stage, expected] of [
      ["confirmed", "confirmed", "已确认"],
      ["running", "video", "出片中"],
      ["running", "deliver", "出片中"],
      ["completed", "completed", "已完成"],
    ]) {
      assert.equal(context.itemZoneStatus({
        status, stage, coverStage, prepReady: true, renderQueued: true,
      }), expected);
    }
    assert.equal(context.prepPhase({
      status: "running", stage: "download", coverStage,
    }), "下载源视频");
    assert.equal(context.prepPhase({
      status: "confirmed", prepReady: false, coverStage,
      logs: [{ message: "抖音封面请求" }],
    }), "检查人物图与发布文案");
  }
});
