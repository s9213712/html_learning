"""Frontend smoke checks for compact ComfyUI workflow template selection."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def _read(path):
    return (ROOT / path).read_text(encoding="utf-8")


def test_template_details_are_hidden_until_template_selected():
    html = _read("public/index.html")
    js = _read("public/js/36-comfyui-workflows.js")
    css = _read("public/styles.css")

    assert 'id="comfyui-template-summary" class="comfyui-template-summary" hidden' in html
    assert 'id="comfyui-template-panels" class="comfyui-template-panels" hidden' in html
    assert "summary.hidden = true;" in js
    assert "host.hidden = true;" in js
    assert "summary.hidden = false;" in js
    assert "host.hidden = false;" in js
    assert ".comfyui-template-detail-panel" in css


def test_workflow_template_library_cards_are_collapsed_until_opened():
    js = _read("public/js/36-comfyui-workflows.js")
    css = _read("public/styles.css")

    assert '<details class="comfyui-workflow-item">' in js
    assert '<summary class="comfyui-workflow-item-summary">' in js
    assert '<div class="comfyui-workflow-item-body">' in js
    assert ".comfyui-workflow-item > summary" in css
    assert ".comfyui-workflow-item-body" in css
    assert ".comfyui-workflow-expand-indicator::before" in css


def test_workflow_template_cards_show_default_model_notice():
    js = _read("public/js/36-comfyui-workflows.js")
    css = _read("public/styles.css")

    assert 'const COMFYUI_BUILTIN_VAE_LABEL = "使用各自大模型內建 VAE";' in js
    assert "function comfyuiWorkflowDefaultModelEntries" in js
    assert "function comfyuiWorkflowUsesBuiltinVae" in js
    assert "function comfyuiWorkflowDefaultModelSummaryText" in js
    assert "function comfyuiWorkflowDefaultModelNoticeHtml" in js
    assert "預設模型：" in js
    assert 'addEntry("vae", COMFYUI_BUILTIN_VAE_LABEL);' in js
    assert "default_params" in js
    assert "required_models" in js
    assert "required_loras" in js
    assert "required_controlnets" in js
    assert "comfyuiWorkflowDefaultModelNoticeHtml(item, 4)" in js
    assert "comfyuiWorkflowDefaultModelNoticeHtml(detail, 8)" in js
    assert ".comfyui-workflow-default-model-notice" in css


def test_workflow_layout_builder_secondary_actions_are_collapsed():
    html = _read("public/index.html")
    css = _read("public/styles.css")

    assert 'class="comfyui-workflow-action-bar"' in html
    assert 'class="drive-file-actions comfyui-workflow-primary-actions"' in html
    assert 'id="comfyui-workflow-open-visual-btn"' in html
    assert 'id="comfyui-workflow-import-btn"' in html
    assert 'id="comfyui-workflow-update-btn"' in html
    assert '<details class="comfyui-workflow-action-menu">' in html
    assert '<summary class="btn comfyui-workflow-more-summary">更多操作</summary>' in html
    assert 'id="comfyui-workflow-new-btn"' in html
    assert 'id="comfyui-workflow-starter-txt2img-btn"' in html
    assert 'id="comfyui-workflow-load-visual-btn"' in html
    assert 'id="comfyui-workflow-export-current-btn"' in html
    assert 'id="comfyui-workflow-reset-btn"' in html

    assert ".comfyui-workflow-action-bar" in css
    assert ".comfyui-workflow-action-menu-body" in css
    assert ".comfyui-workflow-action-menu[open] > summary::after" in css


def test_direct_template_fields_are_editable_for_large_model_workflows():
    js = _read("public/js/36-comfyui-workflows.js")
    css = _read("public/styles.css")
    assert 'return { kind: "direct", fieldId: field.id };' in js
    assert 'binding.kind === "direct"' in js
    assert "comfyuiTemplateDirectHint" in js
    assert ".comfyui-template-direct-hint" in css


def test_template_loader_model_fields_do_not_reuse_global_vae_select():
    js = _read("public/js/36-comfyui-workflows.js")
    assert 'field?.class_type === "VAELoader" && field?.input_name === "vae_name") ||' in js
    assert 'return { kind: "readonly", editableLockedModel: true };' in js
    assert 'field?.class_type === "VAELoader" && field?.input_name === "vae_name") return { kind: "field", targetId: "comfyui-vae-select" };' not in js
    assert 'binding.targetId === "comfyui-vae-select"' not in js
    assert "comfyuiTemplateDisplayValue(field, field?.current_value)" in js
    assert "留白代表使用各自大模型內建 VAE" in js


def test_template_checkpoint_model_fields_keep_template_defaults_until_user_edits():
    js = _read("public/js/36-comfyui-workflows.js")

    assert 'if ((field?.class_type === "LoraLoader" || field?.class_type === "LoraLoaderModelOnly") && field?.input_name === "lora_name") return false;' in js
    assert 'return !!(field?.locked || field?.read_only || field?.lock_reason === "template_default_model");' in js
    assert 'if (field?.class_type === "CheckpointLoaderSimple" && field?.input_name === "ckpt_name") return { kind: "field", targetId: "comfyui-model-select" };' in js
    assert 'if (comfyuiTemplateCanEditLockedModelField(field)) {\n    return { kind: "readonly", editableLockedModel: true };\n  }\n  if (' in js
    assert 'if (comfyuiTemplateCanEditLockedModelField(field) && comfyuiTemplateLockedModelFieldIsEditing(field))' in js
    assert 'data-comfyui-template-model-edit="${sanitize(field.id || "")}"' in js
    assert 'data-comfyui-template-model-reset="${sanitize(field.id || "")}"' in js


def test_template_cards_do_not_sync_through_legacy_manual_fields():
    html = _read("public/index.html")
    js = _read("public/js/36-comfyui-workflows.js")

    assert "執行時只使用下方模板卡片的值" in js
    assert 'let comfyuiTemplateFieldOverrides = {};' in js
    assert 'if (legacy) legacy.style.display = "none";' in js
    assert "hidden.value = el.value" not in js
    assert "setComfyuiFieldValue(binding.targetId" not in js
    assert "通用生成設定" in html
    assert "非 workflow 模板模式使用；選擇模板後只使用模板卡片內的欄位。" in html


def test_share_metadata_is_prompted_only_when_sharing():
    html = _read("public/index.html")
    js = _read("public/js/36-comfyui.js")

    assert "comfyui-share-title" not in html
    assert "comfyui-share-note" not in html
    assert "comfyui-share-title" not in js
    assert "comfyui-share-note" not in js
    assert "function promptComfyuiShareMetadata()" in js
    assert 'window.prompt("分享標題"' in js
    assert 'window.prompt("心得留言（可留空）"' in js


def test_seed_after_generation_mode_is_available_for_generate_and_templates():
    html = _read("public/index.html")
    comfyui_js = _read("public/js/36-comfyui.js")
    workflow_js = _read("public/js/36-comfyui-workflows.js")

    assert 'id="comfyui-seed-after-generate"' in html
    assert '<option value="random">生成後隨機</option>' in html
    assert '<option value="fixed" selected>固定</option>' in html
    assert '<option value="increment">+1</option>' in html
    assert '<option value="decrement">-1</option>' in html
    assert '"comfyui-seed-after-generate"' in comfyui_js
    assert "function applyComfyuiSeedAfterGenerate" in comfyui_js
    assert "function comfyuiSeedAfterGenerateMode" in comfyui_js
    assert "function setComfyuiSeedAfterGenerateMode" in comfyui_js
    assert "randomComfyuiSeedForUi" in comfyui_js
    assert "currentSelectedComfyuiTemplateSeedValue" in comfyui_js
    assert "applyComfyuiSeedAfterGenerate(generated[generated.length - 1]?.seed);" in comfyui_js
    assert 'if (typeof applyComfyuiSeedAfterGenerate === "function") applyComfyuiSeedAfterGenerate(images[images.length - 1]?.seed);' in workflow_js
    assert "function comfyuiTemplateFieldIsSeed" in workflow_js
    assert "function currentSelectedComfyuiTemplateSeedValue" in workflow_js
    assert "function renderComfyuiTemplateSeedAfterGenerateControl" in workflow_js
    assert 'data-comfyui-template-seed-after-generate="1"' in workflow_js
    assert "setComfyuiSeedAfterGenerateMode(select.value)" in workflow_js


def test_template_prompts_default_to_shared_for_multiple_fields():
    workflow_js = _read("public/js/36-comfyui-workflows.js")
    css = _read("public/styles.css")

    assert 'let comfyuiTemplatePromptShareMode = "independent";' in workflow_js
    assert "function comfyuiTemplateNeedsPromptSharingChoice" in workflow_js
    assert "function renderComfyuiTemplatePromptSharingControl" in workflow_js
    assert 'data-comfyui-template-prompt-sharing="1"' in workflow_js
    assert "請先選擇是否全域共用提示詞" in workflow_js
    assert '? \"shared\" : \"independent\"' in workflow_js
    assert ': "shared";' in workflow_js
    assert "syncComfyuiTemplateSharedPromptFields" in workflow_js
    assert 'data-comfyui-template-prompt-role="${sanitize(binding.promptRole)}"' in workflow_js
    assert '["prompt", "tags", "caption"].includes(inputName)' in workflow_js
    assert 'inputName === "string_b"' in workflow_js
    assert 'class="comfyui-template-prompt-sharing-text"' in workflow_js
    assert ".comfyui-template-prompt-sharing" in css
    assert "display: flex;" in css
    assert "flex-wrap: wrap;" in css
    assert ".comfyui-template-prompt-sharing-text" in css
    assert "overflow-wrap: anywhere;" in css
    assert "flex: 0 1 280px;" in css
    assert "width: auto;" in css


def test_compare_two_checkpoints_shares_sampler_params_except_checkpoint_models():
    workflow_js = _read("public/js/36-comfyui-workflows.js")

    assert 'const COMFYUI_COMPARE_TWO_CHECKPOINTS_ID = "origin_compare_2checkpoints";' in workflow_js
    assert "COMFYUI_COMPARE_SHARED_KSAMPLER_INPUTS" in workflow_js
    assert "function comfyuiTemplateIsCompareTwoCheckpoints" in workflow_js
    assert "function comfyuiTemplateIsHiddenCompareSharedField" in workflow_js
    assert "function comfyuiTemplateCompareSharedRuntimeValue" in workflow_js
    assert "共同種子" in workflow_js
    assert "共同取樣器" in workflow_js
    assert "comfyuiTemplateIsHiddenCompareSharedField(detail, field)" in workflow_js
    assert "function comfyuiTemplateIsHiddenSharedPromptField" in workflow_js
    assert "comfyuiTemplateIsHiddenSharedPromptField(detail, field)" in workflow_js
    assert "? comfyuiTemplateSharedPromptValue(comfyuiTemplatePromptRole(field), detail)" in workflow_js
    assert "? comfyuiTemplateCompareSharedRuntimeValue(detail, field)" in workflow_js


def test_multi_compare_checkpoint_template_adds_models_and_loras_dynamically():
    comfyui_js = _read("public/js/36-comfyui.js")
    workflow_js = _read("public/js/36-comfyui-workflows.js")
    css = _read("public/styles.css")

    assert 'const COMFYUI_MULTI_COMPARE_CHECKPOINTS_TEST_ID = "origin_multi_compare_checkpoints_test";' in workflow_js
    assert "const COMFYUI_MULTI_COMPARE_MAX_CHECKPOINTS = 14;" in workflow_js
    assert "function renderComfyuiMultiCompareControl" in workflow_js
    assert "function comfyuiMultiCompareRunSpec" in workflow_js
    assert "function comfyuiTemplateIsMultiCompareCheckpointField" in workflow_js
    assert "function comfyuiTemplateIsHiddenField" in workflow_js
    assert "function comfyuiWorkflowLoraNamesForPromptSync()" in workflow_js
    assert "function addComfyuiMultiCompareLora(detail)" in workflow_js
    assert "function removeComfyuiMultiCompareLoraAt(detail, index)" in workflow_js
    assert "function setComfyuiMultiCompareLoraName(detail, index, name" in workflow_js
    assert "applyComfyuiMultiCompareLoraPromptTerms(nextName)" in workflow_js
    assert "removeComfyuiMultiCompareLoraPromptTerms(previousName)" in workflow_js
    assert 'data-comfyui-multi-compare-add-checkpoint="1"' in workflow_js
    assert 'data-comfyui-multi-compare-add-lora="1"' in workflow_js
    assert "multi_compare: multiCompareSpec || undefined" in workflow_js
    assert "const renderPartialWorkflowResult = async (partialResult)" in workflow_js
    assert "onPartialResult: renderPartialWorkflowResult" in workflow_js
    assert "Multi-Compare 已先顯示" in workflow_js
    assert "options.onPartialResult(job.result, job)" in comfyui_js
    assert "comfyuiTemplateIsMultiCompareCheckpointField(detail, field)" in workflow_js
    assert ".comfyui-multi-compare-card" in css
    assert ".comfyui-multi-compare-row.is-lora" in css


def test_multi_method_upscale_template_has_runtime_breakpoint_selector():
    workflow_js = _read("public/js/36-comfyui-workflows.js")
    css = _read("public/styles.css")

    assert 'const COMFYUI_MULTI_METHOD_UPSCALE_ID = "origin_multi_method_upscale";' in workflow_js
    assert 'const COMFYUI_MULTI_METHOD_UPSCALE_MODE_TEST_ID = "origin_multi_method_upscale_mode_test";' in workflow_js
    assert "function renderComfyuiUpscaleBreakpointControl" in workflow_js
    assert "function comfyuiUpscaleBreakpointRunSpec" in workflow_js
    assert 'data-comfyui-upscale-breakpoint="1"' in workflow_js
    assert 'value="model_upscale"' in workflow_js
    assert 'value="latent_upscale"' in workflow_js
    assert 'value="combined_upscale"' in workflow_js
    assert 'mode: normalizeComfyuiUpscaleBreakpointValue(detail, stage)' in workflow_js
    assert "upscale_breakpoint: upscaleBreakpointSpec || undefined" in workflow_js
    assert "comfyuiTemplateIsHiddenUpscaleBreakpointField(detail, field)" in workflow_js
    assert 'stage === "model_upscale" && ["61", "63"].includes(nodeId)' in workflow_js
    assert ".comfyui-upscale-breakpoint-card" in css


def test_generated_images_show_model_labels_and_share_them():
    comfyui_js = _read("public/js/36-comfyui.js")
    routes_py = _read("routes/comfyui.py")
    css = _read("public/styles.css")

    assert "function comfyuiGeneratedImageLabel" in comfyui_js
    assert "return `${prefix}模型：${modelName}`;" in comfyui_js
    assert "payload.output_label = outputLabel;" in comfyui_js
    assert "comfyui-output-label" in comfyui_js
    assert ".comfyui-output-label" in css
    assert "輸出標籤：" in routes_py


def test_non_image_comfyui_outputs_render_as_playable_media():
    comfyui_js = _read("public/js/36-comfyui.js")
    workflow_js = _read("public/js/36-comfyui-workflows.js")

    assert 'kind === "audio"' in comfyui_js
    assert '<audio controls preload="metadata"><source src="${sanitize(src)}" type="${sanitize(item.mime_type || "audio/mpeg")}"></audio>' in comfyui_js
    assert '<video controls preload="metadata" playsinline><source src="${sanitize(src)}" type="${sanitize(item.mime_type || "video/mp4")}"></video>' in comfyui_js
    assert "const generatedMedia = [];" in comfyui_js
    assert "generatedMedia.push({ ...item, run_index: runIndex, batch_index: batchIndex, run_count: runCount });" in comfyui_js
    assert "comfyuiGeneratedMedia = generatedMedia;" in comfyui_js
    assert "if (comfyuiGeneratedImages.length) comfyuiGeneratedMedia = [];" not in comfyui_js
    assert "} else if (media.length) {\n      renderComfyuiGeneratedMedia(comfyuiGeneratedMedia);\n    }" in workflow_js


def test_template_local_images_are_imported_before_safe_remap_gate():
    comfyui_js = _read("public/js/36-comfyui.js")
    workflow_js = _read("public/js/36-comfyui-workflows.js")

    assert 'apiFetch(API + "/comfyui/import-uploaded-image"' in comfyui_js
    assert 'apiFetch(API + "/comfyui/import-uploaded-video"' in comfyui_js
    assert "function importComfyuiUploadedImage" in comfyui_js
    assert "async function importComfyuiUploadedMedia" in comfyui_js
    assert "setComfyuiInputAssetFromRef(assetKey" in comfyui_js
    assert "cloudFileId: image.cloud_file_id" in comfyui_js
    assert "async function ensureComfyuiTemplateImageAssignments" in workflow_js
    assert ".filter((item) => item.hasLocalFile && item.assetKey)" in workflow_js
    assert "await importer(assetKey);" in workflow_js
    assert "await ensureComfyuiTemplateImageAssignments(templateDetail)" in workflow_js


def test_official_workflow_template_media_defaults_are_auto_assigned():
    workflow_js = _read("public/js/36-comfyui-workflows.js")

    assert 'const COMFYUI_OFFICIAL_TEMPLATE_MEDIA_ASSIGNMENT_PREFIX = "official-template-media:";' in workflow_js
    assert "function comfyuiTemplateOfficialMediaFilename" in workflow_js
    assert "function comfyuiTemplateOfficialMediaAssignment" in workflow_js
    assert "function comfyuiTemplateOfficialMediaPreviewUrl" in workflow_js
    assert '`${API}/comfyui/workflows/official-media/${encodeURIComponent(cleanName)}`' in workflow_js
    assert "assignments[String(field.node_id)] = officialAssignment;" in workflow_js
    assert "officialMediaFilename" in workflow_js
    assert "officialMediaPreviewUrl" in workflow_js
    assert '<video src="${sanitize(officialMediaPreviewUrl)}" controls muted preload="metadata"></video>' in workflow_js
    assert '<img src="${sanitize(officialMediaPreviewUrl)}" alt="${sanitize(fieldLabel || "模板範例圖片")}" />' in workflow_js


def test_workflow_templates_use_unlimited_foreground_wait_without_losing_job_id():
    comfyui_js = _read("public/js/36-comfyui.js")
    workflow_js = _read("public/js/36-comfyui-workflows.js")

    assert "const COMFYUI_GENERATION_TIMEOUT_SECONDS = 0;" in comfyui_js
    assert "const COMFYUI_VIDEO_FOREGROUND_TIMEOUT_SECONDS = 0;" in comfyui_js
    assert "const COMFYUI_INTERRUPT_TIMEOUT_SECONDS = 15;" in comfyui_js
    assert "function createComfyuiForegroundTimeoutError(jobId)" in comfyui_js
    assert "err.comfyuiForegroundTimeout = true;" in comfyui_js
    assert "function comfyuiForegroundTimeoutMessage(err)" in comfyui_js
    assert "不設最長等待上限" in comfyui_js
    assert "job_id: interruptedJobId" in comfyui_js
    assert "signal: interruptRequestController.signal" in comfyui_js
    assert "Promise.race([interruptRequest, interruptTimeout])" in comfyui_js
    assert "已停止等待中斷回應" in comfyui_js
    assert "function comfyuiWorkflowPresetForegroundTimeoutSeconds(item = {})" in workflow_js
    assert 'return outputs.includes("video") ? videoTimeout : baseTimeout;' in workflow_js
    assert "const workflowTimeoutSeconds = comfyuiWorkflowPresetForegroundTimeoutSeconds(preset);" in workflow_js
    assert "const jobId = json.job?.job_id;" in workflow_js
    assert "pollComfyuiJobUntilDone(jobId, controller, workflowTimeoutSeconds, {" in workflow_js
    assert 'label: timedOut ? "已停止前台等待" : "產圖失敗"' in workflow_js


def test_workflow_only_modes_delegate_generate_button_to_selected_template():
    comfyui_js = _read("public/js/36-comfyui.js")
    workflow_js = _read("public/js/36-comfyui-workflows.js")

    assert 'return "圖生影片：先選來源圖片，再在上方 Workflow 模板選擇支援 I2V 的工作流後執行。";' in comfyui_js
    assert "runSelectedComfyuiWorkflowTemplateFromGenerate" in comfyui_js
    assert "await runSelectedComfyuiWorkflowTemplateFromGenerate(payload.generation_mode);" in comfyui_js
    assert "function runSelectedComfyuiWorkflowTemplateFromGenerate(mode)" in workflow_js
    assert "function comfyuiWorkflowPresetSupportsMode" in workflow_js
    assert 't2a: "t2s"' in comfyui_js
    assert '["audio", "audios", "music", "song", "songs", "sound", "sounds"]' in workflow_js
    assert "const matches = comfyuiWorkflowPresetsForMode(normalized);" in workflow_js
    assert "await runComfyuiWorkflowPreset(presetId);" in workflow_js
    assert "select.focus();" in workflow_js
    assert "const selectedTemplateId = Number(comfyuiSelectedTemplatePresetId || $(\"comfyui-template-select\")?.value || 0);" in comfyui_js
    assert "await runSelectedComfyuiWorkflowTemplateFromGenerate(payload.generation_mode);" in comfyui_js


def test_generation_mode_dropdown_is_hidden_and_system_inferred():
    html = _read("public/index.html")
    comfyui_js = _read("public/js/36-comfyui.js")
    css = _read("public/styles.css")

    assert '<select id="comfyui-generation-mode" hidden aria-hidden="true" tabindex="-1">' in html
    assert "不用手選文生圖或圖生圖" in html
    assert "function inferComfyuiGenerationMode()" in comfyui_js
    assert 'if (comfyuiHasInputAsset("source")) {' in comfyui_js
    assert 'if (comfyuiHasInputAsset("mask")) return "inpaint";' in comfyui_js
    assert 'return "img2img";' in comfyui_js
    assert ".comfyui-auto-mode-display" in css


def test_template_renderer_relabels_ambiguous_existing_manifest_fields():
    workflow_js = _read("public/js/36-comfyui-workflows.js")
    assert "function comfyuiTemplateFieldLabel" in workflow_js
    assert 'return "正向提示詞";' in workflow_js
    assert 'return "負面提示詞";' in workflow_js
    assert 'return "Checkpoint / 大模型";' in workflow_js
    assert 'return comfyuiTemplateLabelWithNoise(field, "Diffusion / UNet 大模型");' in workflow_js
    assert 'return comfyuiTemplateLabelWithNoise(field, "LoRA 模型（Model-only）");' in workflow_js
    assert 'return "放大 / Upscale 模型";' in workflow_js
    assert "const fieldLabel = comfyuiTemplateFieldLabel(field, binding);" in workflow_js


def test_comfyui_tools_are_split_into_subviews():
    html = _read("public/index.html")
    js = _read("public/js/36-comfyui.js")
    css = _read("public/styles.css")

    assert 'data-comfyui-view="generate"' in html
    assert 'data-comfyui-view="history"' in html
    assert 'data-comfyui-view="workflow"' in html
    assert 'data-comfyui-view="models" hidden' in html
    assert 'data-comfyui-view-panel="generate"' in html
    assert 'data-comfyui-view-panel="history"' in html
    assert 'data-comfyui-view-panel="workflow"' in html
    assert 'data-comfyui-view-panel="models"' in html
    assert "function setComfyuiView" in js
    assert "bindComfyuiSubnav" in js
    assert ".comfyui-subview[hidden]" in css


def test_root_model_management_stays_visible_across_comfyui_modes():
    html = _read("public/index.html")
    js = _read("public/js/36-comfyui.js")
    css = _read("public/styles.css")

    assert 'data-comfyui-view="models" hidden' in html
    assert "function canManageComfyuiLocalModels" in js
    assert 'return currentUser === "root";' in js
    assert 'const modelsUnavailable = selected === "models" && (!canManageComfyuiLocalModels() || (modelTab && modelTab.hidden));' in js
    assert "const showLocalModels = canManageComfyuiLocalModels(mode);" in js
    assert 'if (panel) panel.style.display = showLocalModels ? "" : "none";' in js
    assert "if (modelsTab) modelsTab.hidden = !showLocalModels;" in js
    assert "if (details && !showLocalModels) details.open = false;" in js
    assert "Civitai 匯入區仍可使用" in js
    assert 'document.addEventListener("hackme:account-context-changed"' in js
    assert ".comfyui-subtab[hidden]" in css
