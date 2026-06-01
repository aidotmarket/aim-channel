import { useState } from "react";
import { Cloud, Upload } from "lucide-react";
import DataSourceSettings from "@/components/DataSourceSettings";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useUpload } from "@/contexts/UploadContext";

const ListDataPage = () => {
  const { openModal } = useUpload();
  const [provider, setProvider] = useState("aws");
  const [view, setView] = useState<"upload" | "external">("upload");

  return (
    <div className="space-y-6 animate-in fade-in duration-300">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight text-foreground">List Data</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Add data by uploading files or securely connecting an external source.
        </p>
      </div>

      <div className="flex flex-wrap gap-3">
        <Button
          variant={view === "upload" ? "default" : "outline"}
          className="gap-2"
          onClick={() => setView("upload")}
        >
          <Upload className="h-4 w-4" />
          Upload a file
        </Button>
        <Button
          variant={view === "external" ? "default" : "outline"}
          className="gap-2"
          onClick={() => setView("external")}
        >
          <Cloud className="h-4 w-4" />
          Serve from the cloud
        </Button>
      </div>

      {view === "upload" ? (
        <div className="space-y-4">
          <Card className="bg-card border-border">
            <CardHeader>
              <CardTitle>Upload a file</CardTitle>
              <CardDescription>
                Select files from your computer and add them to your data library.
              </CardDescription>
            </CardHeader>
            <CardContent>
              <Button className="gap-2" onClick={() => openModal()}>
                <Upload className="h-4 w-4" />
                Open upload
              </Button>
            </CardContent>
          </Card>
        </div>
      ) : (
        <div className="space-y-4">
          <Card className="bg-card border-border">
            <CardHeader>
              <CardTitle>Serve from the cloud</CardTitle>
              <CardDescription>
                Connect a source that remains hosted in your cloud account.
              </CardDescription>
            </CardHeader>
            <CardContent className="max-w-md">
              <Select value={provider} onValueChange={setProvider}>
                <SelectTrigger className="bg-background border-border">
                  <SelectValue placeholder="Select a provider" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="aws">Amazon Web Services (S3)</SelectItem>
                  <SelectItem value="google-cloud" disabled>
                    Google Cloud (Coming soon)
                  </SelectItem>
                  <SelectItem value="microsoft-azure" disabled>
                    Microsoft Azure (Coming soon)
                  </SelectItem>
                </SelectContent>
              </Select>
            </CardContent>
          </Card>

          {provider === "aws" ? <DataSourceSettings /> : null}
        </div>
      )}
    </div>
  );
};

export default ListDataPage;
