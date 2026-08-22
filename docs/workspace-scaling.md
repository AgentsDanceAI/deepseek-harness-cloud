# Workspace scaling guide

English | [简体中文](workspace-scaling.zh-CN.md)

This guide defines the technical constraints for moving cloud workspaces from a
single host to elastic infrastructure. Production resource identifiers,
addresses, capacity, prices, and live metrics belong in private operations
systems, not this repository.

## Goals and persistent state

Each user workspace must remain isolated, recoverable after compute stops,
observable, and compatible with the existing workspace API. Capacity exhaustion
must reject new work without impairing the control plane.

The runtime has persistent home and workspace data. A multi-node scheduler
therefore needs shared storage or correct sticky placement, per-user mount
boundaries, and an out-of-band way to retrieve artifacts after compute stops.

## Backend contract

An elastic backend must:

1. create or resume a user's instance;
2. report state and a trusted access address;
3. report ready only after the application serves traffic;
4. release compute while retaining persistent data; and
5. return diagnosable timeout, capacity, network, and mount failures.

`WORK_IMAGE_REF` must be immutable. Release checks keep runtime, image, and cache
versions aligned.

## Security and network

- permit workspace ports only from the trusted proxy/control plane;
- use private networking and least-privilege security rules;
- do not treat Host rewriting or loopback checks as cross-host authentication;
- grant cloud credentials only the resource operations the backend needs;
- isolate user-controlled code and release temporary public resources on every
  success and failure path;
- design outbound dependency installation as a separate egress policy.

## Capacity, readiness, and storage

Admission must consider CPU, memory, instance, address, interface, volume,
storage-throughput, and control-plane API quotas. Reserve headroom for releases
and recovery. Cold-start measurements run from the create API call until the
workspace port returns a valid response and cover cached/uncached images, first
mounts, resume, and representative dependency installs.

Before rollout, write both persistent paths, release and recreate the user,
verify restoration, prove a second user cannot read the data, test missing-path
failure, and measure representative small-file workloads.

## Progressive rollout

Validate lifecycle, isolation, and fault injection outside production; start a
small pool with immutable images and independent network policy; observe
readiness, error rate, quota, storage, and resource cleanup; expand by user or
tenant; retain a configuration rollback to the prior backend.
