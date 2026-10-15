// Generate a non-secret offline fixture with the provider's actual pinned API.
// Run only in the scratch module described in BOOTSTRAP-ADDENDUM.md.
package main

import (
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"os"

	"github.com/cloudbase/garm-provider-common/cloudconfig"
	"github.com/cloudbase/garm-provider-common/params"
	"gopkg.in/yaml.v3"
)

func main() {
	if len(os.Args) != 2 {
		panic("usage: render-fixture <non-secret source extra-specs.json>")
	}
	data, err := os.ReadFile(os.Args[1])
	must(err)
	var spec struct {
		RunnerInstallTemplate []byte `json:"runner_install_template"`
	}
	must(json.Unmarshal(data, &spec))
	if len(spec.RunnerInstallTemplate) == 0 {
		panic("missing template")
	}
	script, err := cloudconfig.InstallRunnerScript(cloudconfig.InstallRunnerParams{
		RunnerUsername: "runner", RunnerGroup: "runner", RunnerName: "garm-iso-fixture",
		RunnerLabels: "self-hosted,garm-managed", RepoURL: "https://github.com/example/private",
		MetadataURL: "https://controller.invalid/metadata", CallbackURL: "https://controller.invalid/status",
		CallbackToken: "fixture-bootstrap",
	}, params.Linux, string(spec.RunnerInstallTemplate))
	must(err)
	cfg, err := cloudconfig.GetCloudInitConfig(params.BootstrapInstance{
		ExtraSpecs: data, UserDataOptions: params.UserDataOptions{DisableUpdatesOnBoot: true},
	}, script)
	must(err)
	var parsed map[string]interface{}
	must(yaml.Unmarshal([]byte(cfg), &parsed))
	// Record the actual generated config, not a handcrafted runcmd model.
	out := map[string]interface{}{
		"common_commit": "bbcacae60e7cfc249013a9d4d371c4144046a44c",
		"template_source_sha256": fmt.Sprintf("%x", sha256.Sum256(spec.RunnerInstallTemplate)),
		"cloud_config": parsed,
		"rendered_template_b64": base64.StdEncoding.EncodeToString(script),
	}
	encoded, err := json.MarshalIndent(out, "", "  ")
	must(err)
	fmt.Println(string(encoded))
}

func must(err error) {
	if err != nil {
		panic(err)
	}
}
