fn main() {
    let proto = "../protobuf/qicardmesh.proto";
    println!("cargo:rerun-if-changed={proto}");
    prost_build::Config::new()
        .type_attribute(".", "#[derive(serde::Serialize, serde::Deserialize)]")
        .compile_protos(&[proto], &["../protobuf"])
        .expect("failed to compile protobuf schema");
}
