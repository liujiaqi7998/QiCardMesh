//! QiCardMeshAgent library. The binary in `main.rs` only wires these modules
//! together, which keeps the card and protocol code straightforward to test.

pub mod cards;
pub mod config;
pub mod identity;
pub mod mqtt;

pub mod proto {
    include!(concat!(env!("OUT_DIR"), "/qicardmesh.rs"));
}
