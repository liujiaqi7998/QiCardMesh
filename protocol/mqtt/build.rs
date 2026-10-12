fn main() {
    // Use the vendored compiler so Windows, macOS and Linux builds do not depend
    // on a system-wide protoc installation.
    let protoc = protoc_bin_vendored::protoc_bin_path().expect("locate vendored protoc");
    std::env::set_var("PROTOC", protoc);

    println!("cargo:rerun-if-changed=qicardmesh.proto");
    prost_build::Config::new()
        .compile_protos(&["qicardmesh.proto"], &["."])
        .expect("compile qicardmesh MQTT protobuf");
}
