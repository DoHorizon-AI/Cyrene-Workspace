// ╔══════════════════════════════════════════════════════════════════════╗
// ║ Module: native_components_v2::kernel_readonly_probe                  ║
// ║ Role: Read Kernel capabilities and update readiness over its UDS.      ║
// ║                                                                      ║
// ║ 模块职责：只读查询 Kernel 能力与更新就绪状态，不调用变更 RPC。          ║
// ╚══════════════════════════════════════════════════════════════════════╝

use std::{env, path::PathBuf, process, time::Duration};

use cy_proto::{core_v1, core_v2};
use hyper_util::rt::TokioIo;
use serde_json::{json, Value};
use tokio::net::UnixStream;
use tonic::{transport::Endpoint, Request};
use tower::service_fn;

/// Build a channel to an already-running Kernel UDS without starting a service.
///
/// 中文：连接已运行的 Kernel UDS，不启动任何服务。
async fn connect_kernel(socket_path: PathBuf) -> Result<tonic::transport::Channel, String> {
    let endpoint = Endpoint::try_from("http://[::]:50051")
        .map_err(|error| format!("invalid local endpoint: {error}"))?;
    endpoint
        .connect_with_connector(service_fn(move |_| {
            let socket_path = socket_path.clone();
            async move { UnixStream::connect(socket_path).await.map(TokioIo::new) }
        }))
        .await
        .map_err(|error| format!("UDS connection failed: {error}"))
}

/// Read global Kernel capabilities, excluding node and device identifiers.
///
/// 中文：只读获取 Kernel 全局能力，并省略节点与设备标识符。
async fn query_capabilities(channel: tonic::transport::Channel) -> Value {
    let mut client = core_v1::kernel_service_client::KernelServiceClient::new(channel);
    let mut request = Request::new(core_v1::GetKernelCapabilitiesRequest {
        context: None,
        node: None,
    });
    request.set_timeout(Duration::from_secs(4));
    match client.get_kernel_capabilities(request).await {
        Ok(response) => {
            let capabilities = response.into_inner();
            json!({
                "status": "RESPONDED",
                "kernelVersion": capabilities.kernel_version,
                "inventoryGeneration": capabilities.inventory_generation,
                "sandboxBackends": capabilities.sandbox_backends,
                "featureFlags": capabilities.feature_flags,
                "enforcement": capabilities.enforcement.into_iter().map(|fact| json!({
                    "resourceKind": fact.resource_kind,
                    "mode": fact.mode,
                    "adapterId": fact.adapter_id,
                    "reasonCode": fact.reason_code,
                })).collect::<Vec<_>>(),
            })
        }
        Err(status) => json!({"status": "UNKNOWN", "rpcCode": format!("{:?}", status.code())}),
    }
}

/// Read task, Worker, Lease, and allocation readiness without mutating state.
///
/// 中文：只读查询任务、Worker、Lease 与分配状态，不改变状态。
async fn query_readiness(channel: tonic::transport::Channel) -> Value {
    let mut client =
        core_v2::kernel_authority_service_client::KernelAuthorityServiceClient::new(channel);
    let mut request = Request::new(core_v2::UpdateReadinessRequest {
        target_kind: core_v2::MaintenanceTargetKind::PackageOnly as i32,
        expected_activity_sources: Vec::new(),
        expected_catalog_generation: 0,
        requires_restart: false,
    });
    request.set_timeout(Duration::from_secs(4));
    match client.get_update_readiness(request).await {
        Ok(response) => {
            let readiness = response.into_inner();
            let active_task_states = readiness
                .active_tasks
                .iter()
                .map(|task| task.state)
                .collect::<Vec<_>>();
            json!({
                "status": "RESPONDED",
                "readinessStatus": readiness.status,
                "gateGeneration": readiness.gate_generation,
                "activeTaskCount": readiness.active_task_count,
                "activeWorkerCount": readiness.active_worker_count,
                "activeLeaseOrAllocationCount": readiness.active_lease_or_allocation_count,
                "inflightRuntimeAdmissionCount": readiness.inflight_runtime_admission_count,
                "unknownActivitySourceCount": readiness.unknown_activity_sources.len(),
                "blockerCodes": readiness.blocker_codes,
                "activeTaskStates": active_task_states,
                "applyAdmissionDecision": "CLOSED",
            })
        }
        Err(status) => json!({"status": "UNKNOWN", "rpcCode": format!("{:?}", status.code())}),
    }
}

/// Parse the one required socket argument and emit a sanitized JSON receipt.
///
/// 中文：解析唯一必需的 socket 参数并输出已净化的 JSON 回执。
#[tokio::main]
async fn main() {
    let arguments = env::args().collect::<Vec<_>>();
    if arguments.len() != 3 || arguments[1] != "--socket" {
        eprintln!("usage: cyrene-kernel-readonly-probe --socket /absolute/kernel.sock");
        process::exit(64);
    }
    let socket_path = PathBuf::from(&arguments[2]);
    if !socket_path.is_absolute() {
        eprintln!("Kernel socket path must be absolute");
        process::exit(64);
    }
    let channel = match connect_kernel(socket_path.clone()).await {
        Ok(channel) => channel,
        Err(_error) => {
            println!(
                "{}",
                json!({
                    "schemaVersion": 1,
                    "socketPath": socket_path,
                    "capabilities": {"status": "UNKNOWN", "errorClass": "UDS_CONNECTION_FAILED"},
                    "readiness": {"status": "UNKNOWN", "rpcCode": "NOT_ATTEMPTED"},
                    "taskLeaseWorkerState": "UNKNOWN",
                    "gpuIdleConclusion": "UNKNOWN",
                    "applyAdmissionDecision": "CLOSED",
                })
            );
            return;
        }
    };
    let capabilities = query_capabilities(channel.clone()).await;
    let readiness = query_readiness(channel).await;
    let readiness_responded = readiness["status"] == "RESPONDED";
    println!(
        "{}",
        json!({
            "schemaVersion": 1,
            "socketPath": socket_path,
            "capabilities": capabilities,
            "readiness": readiness,
            "taskLeaseWorkerState": if readiness_responded { "OBSERVED" } else { "UNKNOWN" },
            "gpuIdleConclusion": "UNKNOWN",
            "applyAdmissionDecision": "CLOSED",
            "limits": [
                "Only GetKernelCapabilities and GetUpdateReadiness were invoked.",
                "The probe did not acquire or release a Lease, start or stop a Worker, or launch a process.",
                "Kernel readiness is not a per-request hard GPU binding proof.",
                "Task and resource identifiers and token material are not emitted.",
            ],
        })
    );
}
